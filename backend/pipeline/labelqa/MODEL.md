# Label-quality models

The scoring stage (`python -m pipeline.estimate_label_quality`) scores with one
of the three checkpoints released in the
[label-quality-assurance](https://github.com/piculjantechnologies/label-quality-assurance)
repository, selected by `LABELQA_MODEL`: `unified` (the default), `thesis` or
`paper`. Given an image and its boxes of the 20 Pascal VOC classes, the model
returns the probability that the label set as a whole (every box and its
class) is correct. The stage stores it as `label_quality_score`, and the first
12 hex digits of the checkpoint's SHA-256 as `label_quality_model`.

| `LABELQA_MODEL` | `unified` (default) | `thesis` | `paper` |
| --- | --- | --- | --- |
| Source folder | `unified/` | `thesis/` | `paper/` |
| Trained on | Pascal VOC, 20 classes | Pascal VOC, 20 classes | COCO, 80 classes |
| Input size | 224 px | 224 px | 640 px |
| Network | `FusionNet`: ResNet-50 image trunk, ResNet-18 label trunk, per-box head | `CorrNet` with the per-cell label-plane head | `CorrNet` with the raster head |
| Checkpoint | [`unified__artifacts_voc__best_ema_calibrated.pth`](https://labelqa.blob.core.windows.net/checkpoints/v1.0/unified__artifacts_voc__best_ema_calibrated.pth) | [`thesis__artifacts__best_model_refit.pth`](https://labelqa.blob.core.windows.net/checkpoints/v1.0/thesis__artifacts__best_model_refit.pth) | [`paper__artifacts__best_model_refit.pth`](https://labelqa.blob.core.windows.net/checkpoints/v1.0/paper__artifacts__best_model_refit.pth) |
| Size | 154,517,793 bytes (489 tensors) | 105,951,820 bytes (285 tensors) | 107,326,509 bytes (288 tensors) |
| SHA-256 | `738531d0923755983ac7fb697c48f5d40ef23083b3dc6c37f3f78ef4b616b757` | `27040e830f3f927a6f4a797ff857140e5b33826a2129c74dacc60ca30f1590ca` | `5727521cb7712774df8651e438dc70c1f08a91972bebd6e909d99f486012a9e4` |
| `label_quality_model` | `738531d09237` | `27040e830f3f` | `5727521cb771` |
| Default `--batch` | 32 | 32 | 4 |

All three come from the source repository at commit `07f31da` and are licensed
AGPL-3.0-only.

**Which model.** Cortex uses `unified` by default: it was trained on Pascal
VOC, the classes the detection stage keeps, and besides judging the label as a
whole it scores every box with its per-box head. `thesis` is the other Pascal
VOC model. `paper` was trained on COCO, where a good label has a box for every
object of the 80 COCO classes; Cortex stores boxes of the 20 Pascal VOC classes
only, so an image that shows objects of the other 60 classes lacks their boxes
and tends to score lower with this model.

**Switching.** Set `LABELQA_MODEL` for the scoring stage and run it with
`--rescore`: the documents whose `label_quality_model` is not the selected
checkpoint's are scored again (see [PIPELINE.md](../../docs/PIPELINE.md)).
Scores of different models are not comparable, so a collection should be
scored with one model.

## Code

Each subpackage vendors what inference needs from its source folder, verbatim;
each file starts with a header naming its source, and apart from that only the
imports were trimmed to what the copied parts use.

| Subpackage | Copied from | Fed as |
| --- | --- | --- |
| [`unified/`](unified) | `network.py`: `STAGES`, `WORST_Q`, `LEVELS_FOR_STRIDE`, `FREEZE_CHOICES`, `Trunk` and `FusionNet` of `unified/qa_model.py`; `preprocessing.py`: the rendering constants and `resize_keep_aspect`, `center_pad`, `normalize_image`, `render_plane`, `render_planes`, `get_sample` and `box_rois` of `unified/qa_data.py`; `voc.py`: the constants of `unified/dataset_voc.py`, which stand in for the source's `dataset` module with `QA_DATASET=voc` | `unified/qa_demo.py` (`score`) |
| [`thesis/`](thesis) | `network.py`: `CorrNet` of `thesis/neural_network.py`; `preprocessing.py`: `VOC_CLASSES`, `RES`, `MEAN`, `STD`, ... and `resize_keep_aspect`, `center_pad`, `draw_box`, `render_planes` and `normalize_image` of `thesis/data_loader.py` | `thesis/analysis.py` (`score`) |
| [`paper/`](paper) | `network.py`: `NUM_CLASSES`, `_trunk` and `Net` of `paper/neural_network.py` and `CorrNet` of `paper/coco_corrnet.py`; `preprocessing.py`: `COCO_CLASSES` of `paper/interactive_demo.py` and `NUM_CLASSES` and `get_sample` of `paper/data_loader.py` | `paper/web_demo.py` (`classify`) |

Each subpackage's `__init__.py` describes its checkpoint and holds the glue
between Cortex's documents and the copied code: `prepare` (image and boxes to
model inputs), `forward` (a batch of inputs through the network) and `build`
(the network from the checkpoint). `load_model()` in
[`__init__.py`](__init__.py) reads the selected checkpoint, checks its
SHA-256, and builds the network from exactly those bytes, with
`torch.load(..., weights_only=True)` and a strict `load_state_dict`, as the
source builds it (`thesis/neural_network.py`'s `load_model`,
`CorrNet.from_checkpoint`, `FusionNet.from_checkpoint`). For the unified model
the bytes are loaded with `weights_only=True` before `FusionNet.from_checkpoint`
reads them, so its fallback to a full unpickle is never reached. No weight is
downloaded from torchvision: every weight comes from the checkpoint. The
network is returned in eval mode with gradients off.

The tests in `tests/pipeline/test_pipeline_labelqa.py` pin each model's
preprocessing, and with the checkpoints on the machine its scores, to golden
values computed with the source repository's code.

Change a model's network, preprocessing or checkpoint only together.
`label_quality_model` identifies the checkpoint, so after a switch to another
checkpoint `--rescore` scores the collection again; after a change of the code
alone, remove `label_quality_model` from the documents before running
`--rescore`.

## Checkpoint download and cache

The checkpoints are not stored in this repository. `load_model()` resolves the
selected model's checkpoint as follows:

1. `LABELQA_CHECKPOINT`, when set, names the file to load.
2. Otherwise the cache file
   `${XDG_CACHE_HOME:-~/.cache}/cortex/labelqa/<hash prefix>-<file name>` is
   used, for example `738531d09237-best_ema_calibrated.pth` (the expected
   hash's first 12 hex digits and the release's file name). When it is
   missing, the checkpoint is downloaded from the URL in the table above (or
   `LABELQA_CHECKPOINT_URL`): the body is streamed into a temporary file in
   the cache directory, its SHA-256 is checked, and only a matching file is
   renamed to the cache file (an atomic rename). A mismatch or a failed
   download stops the stage with an error and leaves nothing behind.
3. Every load reads the whole file, checks its SHA-256 against the expected
   hash, and builds the network from exactly those bytes. A file that does
   not match is never loaded; this applies to `LABELQA_CHECKPOINT` files and
   to the cache alike.

| Variable | Effect |
| --- | --- |
| `LABELQA_MODEL` | `unified` (default), `thesis` or `paper`; any other value stops the stage with an error |
| `LABELQA_CHECKPOINT` | path of a local checkpoint file of the selected model to load instead of the cache; it is verified like the cache file |
| `LABELQA_CHECKPOINT_URL` | download URL, for example a mirror (default: the selected model's URL above) |
| `LABELQA_CHECKPOINT_SHA256` | expected SHA-256 (default: the selected model's hash above). Set it only to load another checkpoint of the selected model's network; `label_quality_model` then holds its first 12 hex digits. |

The download is a plain HTTPS request to the checkpoint's host (connect
timeout 10 s, 60 s per read); it does not go through the crawl client. On a
host without internet access, download and check the file elsewhere and point
`LABELQA_CHECKPOINT` at it:

```sh
curl -fL -o best_ema_calibrated.pth https://labelqa.blob.core.windows.net/checkpoints/v1.0/unified__artifacts_voc__best_ema_calibrated.pth
sha256sum best_ema_calibrated.pth   # 738531d0923755983ac7fb697c48f5d40ef23083b3dc6c37f3f78ef4b616b757
```

## Inputs and score

For every model the stage decodes the image bytes with OpenCV (BGR, EXIF
orientation applied) and takes the boxes of `object_detection` (normalised
`[x1, y1, x2, y2]` per Pascal VOC class name); class names outside the 20
classes are skipped. A document without boxes of a Pascal VOC class
(`object_detection: {}`) is not scored: the stage stores `null` with
`label_quality_error: "no detections"`. The model would judge the empty label
set, which can be the right label for an image, and such a score would list
images without boxes in the web app's results. The score is
`softmax(logits)[1]`, the probability of output 1 ("good label"), a number
from 0 to 1.

**unified**, as `unified/qa_demo.py` scores a sample:

1. The boxes are multiplied by the image's width and height (original-image
   pixels, keyed by class name).
2. `get_sample` scales the image so that its longer side is 224 px (area
   interpolation when shrinking, bilinear when enlarging), centres it on a
   black 224 × 224 canvas, converts it to RGB, scales it to [0, 1] and
   normalises it with the ImageNet mean and standard deviation. It draws each
   box, letterboxed the same way, into its class's plane: filled interior
   (0.25), border and both diagonals (1.0), 20 planes of 224 × 224.
3. `box_rois` gives each box's letterboxed coordinates and class for the
   per-box head; the stage prepends the sample's index in the batch.
4. The network is called as `FusionNet(planes, image, boxes=rows)`.

**thesis**, as `thesis/analysis.py` (`score`) does:

1. The image is converted to RGB, the channel order of the source's data
   loader, and `resize_keep_aspect` scales it so that its longer side is
   224 px (nearest-neighbour).
2. `normalize_image` centres it on a black 224 × 224 canvas, scales it to
   [0, 1], normalises it with the ImageNet mean and standard deviation (RGB
   order) and returns it channels-first.
3. The boxes are multiplied by the resized image's width and height and
   clipped to [0, width − 1] and [0, height − 1]. `render_planes` draws each
   box (outline and both diagonals) into its class's plane, centred like the
   image: 20 planes of 224 × 224.
4. The network is called as `CorrNet(image, planes)`.

**paper**, as `paper/web_demo.py` (`classify`) does:

1. The six Pascal VOC class names whose COCO spelling differs are renamed
   (aeroplane → airplane, diningtable → dining table, motorbike → motorcycle,
   pottedplant → potted plant, sofa → couch, tvmonitor → tv), and the boxes
   become `[x, y, w, h]` in original-image pixels.
2. `get_sample(640, ...)` scales the image so that its longer side is 640 px
   (bilinear) and centres it on a black 640 × 640 canvas, unnormalised and in
   BGR order (the network normalises it). It draws each box's 1-px border into
   its class's plane: 80 planes of 640 × 640.
3. The network is called as `CorrNet(planes, image)`.

Documents are scored in batches (`--batch`, default per model above; one
paper-model sample is about 136 MB of input, hence its smaller batch). In eval
mode every sample is scored independently, so a batch gives the same scores
as single calls up to floating-point rounding.

## Published results

Each checkpoint's evaluation is recorded in the source repository. The figures
were measured on dataset images with generated candidate labels, each under
its folder's own protocol, not on the web images and detector boxes that
Cortex scores, and they are not comparable across the three models.

| Model | Evaluation | Accuracy | ROC-AUC | Record |
| --- | --- | --- | --- | --- |
| unified | held-out protocol on the Pascal VOC 2012 large set, 5 717 candidates (the paper's error taxonomy) | 0.857 | 0.916 | `unified/artifacts_voc/protocol.out` |
| thesis | large-set evaluation on Pascal VOC 2012 images, 11 434 candidates | 0.8913 | 0.9424 | `thesis/artifacts/eval.json` |
| paper | the paper's test protocol on COCO train2017, 108 151 candidates | 0.8143 | 0.8824 | `paper/artifacts/eval.json` |

The method is described in:

- Pičuljan, N. and Car, Ž. *Machine Learning-Based Label Quality Assurance for
  Object Detection Projects in Requirements Engineering.* Applied Sciences
  13(10):6234, 2023. [doi:10.3390/app13106234](https://doi.org/10.3390/app13106234)
- Pičuljan, N. *Machine learning-based method for quality assurance of object
  bounding box labels in images.* PhD thesis, University of Zagreb, Faculty of
  Electrical Engineering and Computing, 2025.
  [urn:nbn:hr:168:865107](https://urn.nsk.hr/urn:nbn:hr:168:865107)

## Licence

The vendored code and the checkpoints are the same author's work and are
licensed AGPL-3.0-only, like Cortex and the label-quality-assurance
repository. Each checkpoint's image trunk was initialised from torchvision's
ImageNet-pretrained weights, so some of its tensors are derived from
torchvision's parameters, as the source repository's NOTICE states:

- unified: ResNet-50 (`IMAGENET1K_V2`), frozen through layer2 (the pretrained
  parameters, up to weight-EMA floating-point rounding, at most 6e-3
  absolute), later stages fine-tuned;
- thesis and paper: ResNet-18 (`IMAGENET1K_V1`), fine-tuned, its BatchNorm
  running statistics kept frozen (equal to the pretrained ones up to
  weight-EMA rounding, at most 2e-4 absolute).

torchvision's BSD 3-Clause notice is reproduced in [NOTICE](../../../NOTICE).
