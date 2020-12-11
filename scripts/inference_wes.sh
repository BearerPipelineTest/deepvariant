#!/bin/bash
# Copyright 2020 Google LLC.

set -euo pipefail


USAGE=$'
Example usage:
inference_wes.sh --call_variants_extra_args "use_openvino=true"

Flags:
--docker_build (true|false)  Whether to build docker image. (default: false)
--use_gpu (true|false)   Whether to use GPU when running case study. Make sure to specify vm_zone that is equipped with GPUs. (default: false)
--customized_model Path to checkpoint directory containing model checkpoint.
--regions Regions passed into both variant calling and hap.py. (Not applicable in inference_wes.sh)
--make_examples_extra_args Flags for make_examples, specified as "flag1=param1,flag2=param2".
--call_variants_extra_args Flags for call_variants, specified as "flag1=param1,flag2=param2".
--postprocess_variants_extra_args  Flags for postprocess_variants, specified as "flag1=param1,flag2=param2".
'

# Specify default values.
BUILD_DOCKER=false
USE_GPU=false
REGIONS=""
CUSTOMIZED_MODEL=""
MAKE_EXAMPLES_ARGS=""
CALL_VARIANTS_ARGS=""
POSTPROCESS_VARIANTS_ARGS=""

while (( "$#" )); do
  case "$1" in
    --docker_build)
      BUILD_DOCKER="$2"
      if [[ ${BUILD_DOCKER} != "true" ]] && [[ ${BUILD_DOCKER} != "false" ]]; then
        echo "Error: --docker_build needs to have value (true|false)." >&2
        echo "$USAGE" >&2
        exit 1
      fi
      shift # Remove argument name from processing
      shift # Remove argument value from processing
      ;;
    --use_gpu)
      USE_GPU="$2"
      if [[ ${USE_GPU} != "true" ]] && [[ ${USE_GPU} != "false" ]]; then
        echo "Error: --use_gpu needs to have value (true|false)." >&2
        echo "$USAGE" >&2
        exit 1
      fi
      shift # Remove argument name from processing
      shift # Remove argument value from processing
      ;;
    --regions)
      echo "Error: --regions is not used in inference.wes.sh" >&2
      echo "$USAGE" >&2
      exit 1
      ;;
    --customized_model)
      CUSTOMIZED_MODEL="$2"
      shift # Remove argument name from processing
      shift # Remove argument value from processing
      ;;
    --make_examples_extra_args)
      MAKE_EXAMPLES_ARGS="$2"
      shift # Remove argument name from processing
      shift # Remove argument value from processing
      ;;
    --call_variants_extra_args)
      CALL_VARIANTS_ARGS="$2"
      shift # Remove argument name from processing
      shift # Remove argument value from processing
      ;;
    --postprocess_variants_extra_args)
      POSTPROCESS_VARIANTS_ARGS="$2"
      shift # Remove argument name from processing
      shift # Remove argument value from processing
      ;;
    -*|--*=) # other flags not supported
      echo "Error: unrecognized flag $1" >&2
      echo "$USAGE" >&2
      exit 1
      ;;
    *)
      echo "Error: unrecognized extra args $1" >&2
      echo "$USAGE" >&2
      exit 1
      ;;
  esac
done

echo "========================="
echo "BUILD_DOCKER: $BUILD_DOCKER"
echo "CUSTOMIZED_MODEL: $CUSTOMIZED_MODEL"
echo "MAKE_EXAMPLES_ARGS: $MAKE_EXAMPLES_ARGS"
echo "CALL_VARIANTS_ARGS: $CALL_VARIANTS_ARGS"
echo "POSTPROCESS_VARIANTS_ARGS: $POSTPROCESS_VARIANTS_ARGS"
echo "USE_GPU: $USE_GPU"
echo "========================="

## Preliminaries
# Set a number of shell variables, to make what follows easier to read.
BASE="${HOME}/exome-case-study"
BIN_VERSION="1.1.0"

INPUT_DIR="${BASE}/input/data"
REF="GCA_000001405.15_GRCh38_no_alt_analysis_set.fna"
BAM="HG003.novaseq.wes_idt.100x.dedup.bam"
TRUTH_VCF="HG003_GRCh38_1_22_v4.2_benchmark.vcf.gz"
TRUTH_BED="HG003_GRCh38_1_22_v4.2_benchmark.bed"

N_SHARDS="64"

OUTPUT_DIR="${BASE}/output"
OUTPUT_VCF="HG003.output.vcf.gz"
OUTPUT_GVCF="HG003.output.g.vcf.gz"
LOG_DIR="${OUTPUT_DIR}/logs"

CAPTURE_BED="idt_capture_novogene.grch38.bed"

## Create local directory structure
mkdir -p "${OUTPUT_DIR}"
mkdir -p "${INPUT_DIR}"
mkdir -p "${LOG_DIR}"

declare -a extra_args
declare -a docker_args

if [[ -n $CUSTOMIZED_MODEL ]]
then
  echo "Copy from gs:// path $CUSTOMIZED_MODEL to ${INPUT_DIR}/"
  gsutil cp "${CUSTOMIZED_MODEL}"/model.ckpt.data-00000-of-00001 "${INPUT_DIR}"
  gsutil cp "${CUSTOMIZED_MODEL}"/model.ckpt.index "${INPUT_DIR}"
  gsutil cp "${CUSTOMIZED_MODEL}"/model.ckpt.meta "${INPUT_DIR}"
  extra_args+=( --customized_model "/input/model.ckpt")
else
    echo "No custom model specified."
fi
if [[ -n $MAKE_EXAMPLES_ARGS ]]
then
  extra_args+=( --make_examples_extra_args "${MAKE_EXAMPLES_ARGS}")
fi
if [[ -n $CALL_VARIANTS_ARGS ]]
then
  extra_args+=( --call_variants_extra_args "${CALL_VARIANTS_ARGS}")
fi
if [[ -n $POSTPROCESS_VARIANTS_ARGS ]]
then
  extra_args+=( --postprocess_variants_extra_args "${POSTPROCESS_VARIANTS_ARGS}")
fi

## Download extra packages
# Install aria2 to download data files.
sudo apt-get -qq -y update
sudo apt-get -qq -y install aria2

if ! hash docker 2>/dev/null; then
  echo "'docker' was not found in PATH. Installing docker..."
  # Install docker using instructions on:
  # https://docs.docker.com/install/linux/docker-ce/ubuntu/
  sudo apt-get -qq -y install \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg-agent \
    software-properties-common
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add -
  sudo add-apt-repository \
    "deb [arch=amd64] https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) \
    stable"
  sudo apt-get -qq -y update
  sudo apt-get -qq -y install docker-ce
fi

GCS_DATA_DIR="https://storage.googleapis.com/deepvariant"
# Copy the data
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/exome-case-study-testdata/${BAM}"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/exome-case-study-testdata/${BAM}.bai"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/exome-case-study-testdata/${CAPTURE_BED}"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${TRUTH_BED}"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${TRUTH_VCF}"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${TRUTH_VCF}.tbi"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${REF}.gz"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${REF}.gz.fai"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${REF}.gz.gzi"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${REF}.gzi"
aria2c -c -x10 -s10 -d "${INPUT_DIR}" "${GCS_DATA_DIR}/case-study-testdata/${REF}.fai"

if [[ "${BUILD_DOCKER}" = true ]]
then
  if [[ "${USE_GPU}" = true ]]
  then
    IMAGE="deepvariant_gpu:latest"
    sudo docker build \
      --build-arg=FROM_IMAGE=nvidia/cuda:10.1-cudnn7-devel-ubuntu18.04 \
      --build-arg=DV_GPU_BUILD=1 -t deepvariant_gpu .
    echo "Done building GPU Docker image ${IMAGE}."
    docker_args+=( --gpus 1 )
  else
    IMAGE="deepvariant:latest"
    # Pulling twice in case the first one times out.
    sudo docker build -t deepvariant . --build-arg DV_OPENVINO_BUILD=1 || \
      (sleep 5 ; sudo docker build -t deepvariant . --build-arg DV_OPENVINO_BUILD=1)
    echo "Done building Docker image ${IMAGE}."
  fi
else
  if [[ "${USE_GPU}" = true ]]
  then
    IMAGE="google/deepvariant:${BIN_VERSION}-gpu"
    sudo docker pull "${IMAGE}" || \
      (sleep 5 ; sudo docker pull "${IMAGE}")
    docker_args+=( --gpus 1 )
  else
    IMAGE="google/deepvariant:${BIN_VERSION}"
    sudo docker pull "${IMAGE}" || \
      (sleep 5 ; sudo docker pull "${IMAGE}")
  fi
fi

echo "Run DeepVariant..."
echo "using IMAGE=$IMAGE"
# shellcheck disable=SC2068
(time ( sudo docker run \
  -v "${INPUT_DIR}":"/input" \
  -v "${OUTPUT_DIR}:/output" \
  ${docker_args[@]-} \
  "${IMAGE}" \
  /opt/deepvariant/bin/run_deepvariant \
  --model_type=WES \
  --ref="/input/${REF}.gz" \
  --reads="/input/${BAM}" \
  --regions="/input/${CAPTURE_BED}" \
  --output_vcf="/output/${OUTPUT_VCF}" \
  --output_gvcf="/output/${OUTPUT_GVCF}" \
  --num_shards=${N_SHARDS} \
  --logging_dir="/output/logs" \
  "${extra_args[@]-}"
echo "Done.")) 2>&1 | tee "${LOG_DIR}/deepvariant_runtime.log"
echo

## Evaluation: run hap.py
echo "Start evaluation with hap.py..."
UNCOMPRESSED_REF="${INPUT_DIR}/${REF}"

# hap.py cannot read the compressed fa, so uncompress
# into a writable directory. Index file was downloaded earlier.
zcat <"${INPUT_DIR}/${REF}.gz" >"${UNCOMPRESSED_REF}"

sudo docker pull pkrusche/hap.py
( sudo docker run -i \
-v "${INPUT_DIR}:${INPUT_DIR}" \
-v "${OUTPUT_DIR}:${OUTPUT_DIR}" \
pkrusche/hap.py /opt/hap.py/bin/hap.py \
  "${INPUT_DIR}/${TRUTH_VCF}" \
  "${OUTPUT_DIR}/${OUTPUT_VCF}" \
  -f "${INPUT_DIR}/${TRUTH_BED}" \
  -T "${INPUT_DIR}/${CAPTURE_BED}" \
  -r "${UNCOMPRESSED_REF}" \
  -o "${OUTPUT_DIR}/happy.output" \
  --engine=vcfeval
) 2>&1 | tee "${LOG_DIR}/happy.log"
echo "Done."
