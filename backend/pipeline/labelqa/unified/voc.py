# Vendored from the label-quality-assurance repository,
# https://github.com/piculjantechnologies/label-quality-assurance
# unified/dataset_voc.py at commit 07f31da. AGPL-3.0-only.
#
# The constants below are copied verbatim (load_pool, the training pool
# loader, is left out). They stand in for the repository's `dataset` module
# with QA_DATASET=voc, the configuration of the released VOC checkpoint.
"""The Pascal VOC configuration of the unified checkpoint: 20 classes, 224-px inputs."""

RES = 224            # letterbox size
FUSION_RES = 28      # stride-8 fusion grid at 224

CLASSES = ['aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car',
           'cat', 'chair', 'cow', 'diningtable', 'dog', 'horse',
           'motorbike', 'person', 'pottedplant', 'sheep', 'sofa', 'train',
           'tvmonitor']
NUM_CLASSES = len(CLASSES)

# torchvision's detection models emit COCO category names; map the six
# whose VOC spelling differs (other COCO classes are dropped upstream by
# the MAPPING membership test).
DETECTOR_NAME_MAP = {'motorcycle': 'motorbike', 'airplane': 'aeroplane',
                     'couch': 'sofa', 'potted plant': 'pottedplant',
                     'dining table': 'diningtable', 'tv': 'tvmonitor'}
