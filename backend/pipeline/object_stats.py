"""Per-class statistics of a document's boxes, stored next to object_detection.

The web app filters on how many boxes of a class an image has and how large the
largest one is. Computing that from the box lists inside a query cannot use an
index, so the detection stage stores it:

    object_counts    {class: number of boxes}
    object_max_area  {class: area of the largest box as a fraction of the image, 0..1}
    object_total     number of boxes of all classes

Only classes with at least one box appear in the two maps.
"""


def box_area(box):
    """Area of a normalised [x1, y1, x2, y2] box, clipped to the image."""
    x1, y1, x2, y2 = box
    width = max(0.0, min(x2, 1.0) - max(x1, 0.0))
    height = max(0.0, min(y2, 1.0) - max(y1, 0.0))
    return width * height


def object_stats(object_detection):
    """The three fields above for a {class: [box, ...]} map."""
    counts = {}
    max_area = {}
    for name, boxes in object_detection.items():
        if not boxes:
            continue
        counts[name] = len(boxes)
        max_area[name] = round(max(box_area(box) for box in boxes), 6)
    return {"object_counts": counts, "object_max_area": max_area, "object_total": sum(counts.values())}
