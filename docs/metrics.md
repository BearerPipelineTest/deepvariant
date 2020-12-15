# Runtime and accuracy metrics for all release models

## WGS (Illumina)

### Runtime

Runtime is on HG003 (all chromosomes).

Stage                            | Time (minutes)
-------------------------------- | -----------------
make_examples                    | 97m
call_variants                    | 216m (180m with OpenVINO<sup>[*](#vfootnote1)</sup>)
postprocess_variants (with gVCF) | 88m
total                            | 401m = 6.7 hours

### Accuracy

hap.py results on HG003 (all chromosomes, using NIST v4.2 truth), which was held
out while training.

Type  | # TP    | # FN  | # FP | Recall   | Precision | F1_Score
----- | ------- | ----- | ---- | -------- | --------- | --------
Indel |  501841 |  3069 | 1389 | 0.993922 | 0.997351  | 0.995634
SNP   | 3310730 | 20760 | 6202 | 0.993769 | 0.998131  | 0.995945

## WES (Illumina)

### Runtime

Runtime is on HG003 (all chromosomes).

Stage                            | Time (minutes)
-------------------------------- | -----------------
make_examples                    | 8m
call_variants                    | 2m
postprocess_variants (with gVCF) | 1m
total                            | 11m

### Accuracy

hap.py results on HG003 (all chromosomes, using NIST v4.2 truth), which was held
out while training.

Type  | # TP    | # FN | # FP | Recall   | Precision | F1_Score
----- | ------- | ---- | ---- | -------- | --------- | --------
Indel | 1025    | 28   | 19   | 0.973409 | 0.982126  | 0.977748
SNP   | 25005   | 319  | 169  | 0.987403 | 0.993287  | 0.990337


## PacBio (HiFi)

### Runtime

Runtime is on HG003 (all chromosomes).

Stage                            | Time (minutes)
-------------------------------- | -----------------
make_examples                    | 112m
call_variants                    | 228m (166m with OpenVINO<sup>[*](#vfootnote1)</sup>)
postprocess_variants (with gVCF) | 72m
total                            | 412m = 6.8 hours

### Accuracy

hap.py results on HG003 (all chromosomes, using NIST v4.2 truth), which was held
out while training.

(The input BAM is phased already and DeepVariant was run with
`--use_hp_information=true`.)

Type  | # TP    | # FN | # FP | Recall   | Precision | F1_Score
----- | ------- | ---- | ---- | -------- | --------- | --------
Indel |  501912 | 2998 | 2845 | 0.994062 | 0.994586  | 0.994324
SNP   | 3327712 | 3778 | 2274 | 0.998866 | 0.999318  | 0.999092

## Hybrid (Illumina + PacBio HiFi)

### Runtime

Runtime is on HG003 (all chromosomes).

Stage                            | Time (minutes)
-------------------------------- | -----------------
make_examples                    | 146m
call_variants                    | 230m (185m with OpenVINO<sup>[*](#vfootnote1)</sup>)
postprocess_variants (with gVCF) | 62m
total                            | 438 m = 7.3 hours

### Accuracy

Evaluating on HG003 (all chromosomes, using NIST v4.2 truth), which was held out
while training the hybrid model.

Type  | # TP    | # FN | # FP | Recall   | Precision | F1_Score
----- | ------- | ---- | ---- | -------- | --------- | --------
Indel | 503570  | 1340 | 2149 | 0.997346 | 0.995953  | 0.996649
SNP   | 3327590 | 3900 | 1934 | 0.998829 | 0.999419  | 0.999124

## How to reproduce the metrics on this page

For simplicity and consistency, we report runtime with a
[CPU instance with 64 CPUs](deepvariant-details.md#command-for-a-cpu-only-machine-on-google-cloud-platform)
This is NOT the fastest or cheapest configuration. For more scalable execution
of DeepVariant see the [External Solutions] section.

Use `gcloud compute ssh` to log in to the newly created instance.

Download and run any of the following case study scripts:

redacted

```
# WGS (should take about 7 hours)
curl -O https://raw.githubusercontent.com/google/deepvariant/r1.1/scripts/inference_wgs.sh
bash inference_wgs.sh

# WES (should take less than 20 minutes)
curl -O https://raw.githubusercontent.com/google/deepvariant/r1.1/scripts/inference_wes.sh
bash inference_wes.sh

# PacBio (should take about 7 hours)
curl -O https://raw.githubusercontent.com/google/deepvariant/r1.1/scripts/inference_pacbio.sh
bash inference_pacbio.sh

# Hybrid (should take about 7 hours)
curl -O https://raw.githubusercontent.com/google/deepvariant/r1.1/scripts/inference_hybrid_pacbio_illumina.sh
bash inference_hybrid_pacbio_illumina.sh
```

Runtime metrics are taken from the resulting log after each stage of
DeepVariant, and the accuracy metrics come from the hap.py summary.csv output
file.

<a name="vfootnote1">*</a>: To use OpenVINO on Intel CPUs, run with
`--call_variants_extra_args "use_openvino=true"` with the Docker one-step
command. Also see https://github.com/google/deepvariant/pull/363 for more
details.

[External Solutions]: https://github.com/google/deepvariant#external-solutions
[CPU instance with 64 CPUs]: deepvariant-details.md#command-for-a-cpu-only-machine-on-google-cloud-platform
