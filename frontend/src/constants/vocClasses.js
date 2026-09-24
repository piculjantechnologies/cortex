// The 20 Pascal VOC classes the backend detector produces and the data API accepts.
export const VOC_CLASSES = [
  'aeroplane',
  'bicycle',
  'bird',
  'boat',
  'bottle',
  'bus',
  'car',
  'cat',
  'chair',
  'cow',
  'diningtable',
  'dog',
  'horse',
  'motorbike',
  'person',
  'pottedplant',
  'sheep',
  'sofa',
  'train',
  'tvmonitor',
];

// Bounding-box colour per class in the gallery and the class filters.
export const CLASS_COLORS = {
  aeroplane: '#e03131',
  bicycle: '#1c7ed6',
  bird: '#2f9e44',
  boat: '#7048e8',
  bottle: '#f76707',
  bus: '#0c8599',
  car: '#e64980',
  cat: '#fab005',
  chair: '#8d6e63',
  cow: '#15aabf',
  diningtable: '#ae3ec9',
  dog: '#82c91e',
  horse: '#a61e4d',
  motorbike: '#364fc7',
  person: '#12b886',
  pottedplant: '#5c940d',
  sheep: '#ffa8a8',
  sofa: '#b197fc',
  train: '#495057',
  tvmonitor: '#66d9e8',
};

// Black or white, whichever reads better on the given #rrggbb background.
export function labelTextColor(hex) {
  const value = /^#([0-9a-f]{6})$/i.exec(hex || '');
  if (!value) {
    return '#000';
  }
  const n = parseInt(value[1], 16);
  const luminance = (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  return luminance > 0.6 ? '#000' : '#fff';
}
