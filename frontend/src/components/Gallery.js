import React, { useState } from 'react';
import { CLASS_COLORS, labelTextColor } from '../constants/vocClasses';
import { safeHttpUrl } from '../utils/url';

// Draws the normalised [x1, y1, x2, y2] boxes of each class over the image as rendered
// with object-fit: contain inside a `rendered.width` x `rendered.height` box.
function BoundingBoxes({ objectDetection, imageWidth, imageHeight, rendered }) {
  const { width: containerWidth, height: containerHeight } = rendered;
  const originalAspectRatio = imageWidth / imageHeight;
  const renderedAspectRatio = containerWidth / containerHeight;

  let scaleX;
  let scaleY;
  let offsetX = 0;
  let offsetY = 0;

  if (originalAspectRatio > renderedAspectRatio) {
    scaleX = containerWidth / imageWidth;
    scaleY = scaleX;
    offsetY = (containerHeight - imageHeight * scaleY) / 2;
  } else {
    scaleY = containerHeight / imageHeight;
    scaleX = scaleY;
    offsetX = (containerWidth - imageWidth * scaleX) / 2;
  }

  return Object.keys(objectDetection).flatMap((classname) =>
    objectDetection[classname].map((coords, i) => {
      const color = CLASS_COLORS[classname] || '#000000';
      const left = Math.round(coords[0] * imageWidth * scaleX + offsetX);
      const top = Math.round(coords[1] * imageHeight * scaleY + offsetY);
      const width = Math.round((coords[2] - coords[0]) * imageWidth * scaleX);
      const height = Math.round((coords[3] - coords[1]) * imageHeight * scaleY);

      return (
        <div
          key={`${classname}-${i}`}
          className="bounding-box"
          style={{
            left: `${left}px`,
            top: `${top}px`,
            width: `${width}px`,
            height: `${height}px`,
            border: `2px solid ${color}`,
          }}
        >
          <span
            className="bounding-label"
            style={{ backgroundColor: color, color: labelTextColor(color) }}
          >
            {classname}
          </span>
        </div>
      );
    }),
  );
}

// Label quality bands for the score badge: good >= 80 %, fair >= 50 %, poor below.
function qualityBand(score) {
  if (score >= 0.8) {
    return 'good';
  }
  return score >= 0.5 ? 'fair' : 'poor';
}

function ImageTile({ image }) {
  // Size of the rendered <img>. offsetWidth/offsetHeight ignore the hover transform.
  const [rendered, setRendered] = useState(null);
  const url = safeHttpUrl(image.url);
  const detections =
    image.object_detection && typeof image.object_detection === 'object' ? image.object_detection : null;
  const classes = detections ? Object.keys(detections) : [];
  const alt = classes.length > 0 ? `Image with ${classes.join(', ')}` : 'Scraped image';

  const score = Number(image.label_quality_score);
  const hasScore = image.label_quality_score != null && Number.isFinite(score);

  const handleLoad = (event) => {
    const { offsetWidth, offsetHeight } = event.currentTarget;
    setRendered({ width: offsetWidth, height: offsetHeight });
  };

  return (
    <article className="cortex-card">
      <div className="cortex-image-wrapper">
        {url ? (
          <a href={url} target="_blank" rel="noopener noreferrer">
            <img
              src={url}
              alt={alt}
              className="cortex-image"
              referrerPolicy="no-referrer"
              onLoad={handleLoad}
            />
            {detections && rendered && rendered.width > 0 && rendered.height > 0 && (
              <BoundingBoxes
                objectDetection={detections}
                imageWidth={image.width}
                imageHeight={image.height}
                rendered={rendered}
              />
            )}
          </a>
        ) : (
          <p className="cortex-image-missing">Image unavailable</p>
        )}
      </div>
      <div className="cortex-card-body">
        <ul className="cortex-card-classes" aria-label="Objects">
          {classes.map((name) => (
            <li key={name}>
              <span className="cortex-class-dot" style={{ backgroundColor: CLASS_COLORS[name] || '#000000' }} />
              {detections[name].length > 1 ? `${name} ×${detections[name].length}` : name}
            </li>
          ))}
        </ul>
        <p className="cortex-card-quality">
          <span>Label quality</span>
          <span className={`cortex-quality-badge cortex-quality-${hasScore ? qualityBand(score) : 'none'}`}>
            {hasScore ? `${(score * 100).toFixed(1)}%` : 'n/a'}
          </span>
        </p>
      </div>
    </article>
  );
}

const Gallery = ({ images }) => (
  <div className="cortex-gallery">
    {images.map((image) => (
      <ImageTile key={image._id} image={image} />
    ))}
  </div>
);

export default Gallery;
