// Canvas primitives shared by the layered image editor.

export function makeCanvas(width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.imageSmoothingEnabled = false;
  return canvas;
}

export async function canvasFromBase64(width, height, data) {
  const canvas = makeCanvas(width, height);
  const image = new Image();
  await new Promise((resolve, reject) => {
    image.onload = resolve;
    image.onerror = () => reject(new Error("Unable to decode raster layer."));
    image.src = `data:image/png;base64,${data}`;
  });
  canvas.getContext("2d").drawImage(image, 0, 0);
  return canvas;
}

export function cloneCanvas(source) {
  const clone = makeCanvas(source.width, source.height);
  clone.getContext("2d").drawImage(source, 0, 0);
  return clone;
}

export function canvasBase64(canvas) {
  const dataUrl = canvas.toDataURL("image/png");
  return dataUrl.slice(dataUrl.indexOf(",") + 1);
}

export function compositeFrame(frame, width, height) {
  const output = makeCanvas(width, height);
  const context = output.getContext("2d");
  for (const layer of frame.layers) {
    if (!layer.visible || layer.opacity <= 0) continue;
    context.globalAlpha = layer.opacity;
    context.drawImage(layer.canvas, 0, 0);
  }
  context.globalAlpha = 1;
  return output;
}

export function drawFrame(target, frame, opacity = 1) {
  const context = target.getContext("2d");
  context.save();
  context.globalAlpha = opacity;
  for (const layer of frame.layers) {
    if (!layer.visible || layer.opacity <= 0) continue;
    context.globalAlpha = opacity * layer.opacity;
    context.drawImage(layer.canvas, 0, 0);
  }
  context.restore();
}

export function rgbaFromHex(hex, alpha = 255) {
  const clean = String(hex).replace("#", "");
  return {
    r: Number.parseInt(clean.slice(0, 2), 16) || 0,
    g: Number.parseInt(clean.slice(2, 4), 16) || 0,
    b: Number.parseInt(clean.slice(4, 6), 16) || 0,
    a: Math.max(0, Math.min(255, Number(alpha) || 0)),
  };
}

export function hexFromRgba(color) {
  return `#${[color.r, color.g, color.b]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("")}`;
}

export function paintBrushPixels(
  data,
  width,
  height,
  x,
  y,
  color,
  size = 1,
  erase = false,
) {
  const radius = Math.floor(size / 2);
  const left = x - radius;
  const top = y - radius;
  const right = Math.min(width, left + size);
  const bottom = Math.min(height, top + size);
  for (let currentY = Math.max(0, top); currentY < bottom; currentY += 1) {
    for (let currentX = Math.max(0, left); currentX < right; currentX += 1) {
      const offset = (currentY * width + currentX) * 4;
      data[offset] = erase ? 0 : color.r;
      data[offset + 1] = erase ? 0 : color.g;
      data[offset + 2] = erase ? 0 : color.b;
      data[offset + 3] = erase ? 0 : color.a;
    }
  }
}

function matches(data, offset, color) {
  return (
    data[offset] === color.r &&
    data[offset + 1] === color.g &&
    data[offset + 2] === color.b &&
    data[offset + 3] === color.a
  );
}

function assign(data, offset, color) {
  data[offset] = color.r;
  data[offset + 1] = color.g;
  data[offset + 2] = color.b;
  data[offset + 3] = color.a;
}

export function pixelAt(canvas, x, y) {
  const data = canvas.getContext("2d").getImageData(x, y, 1, 1).data;
  return { r: data[0], g: data[1], b: data[2], a: data[3] };
}

export function drawBrush(canvas, x, y, color, size = 1, erase = false) {
  const context = canvas.getContext("2d");
  const radius = Math.floor(size / 2);
  const left = Math.max(0, x - radius);
  const top = Math.max(0, y - radius);
  const right = Math.min(canvas.width, x - radius + size);
  const bottom = Math.min(canvas.height, y - radius + size);
  if (right <= left || bottom <= top) return;
  const image = context.getImageData(left, top, right - left, bottom - top);
  paintBrushPixels(
    image.data,
    image.width,
    image.height,
    x - left,
    y - top,
    color,
    size,
    erase,
  );
  context.putImageData(image, left, top);
}

export function drawLine(canvas, from, to, color, size = 1, erase = false) {
  let x0 = from.x;
  let y0 = from.y;
  const x1 = to.x;
  const y1 = to.y;
  const dx = Math.abs(x1 - x0);
  const sx = x0 < x1 ? 1 : -1;
  const dy = -Math.abs(y1 - y0);
  const sy = y0 < y1 ? 1 : -1;
  let error = dx + dy;
  while (true) {
    drawBrush(canvas, x0, y0, color, size, erase);
    if (x0 === x1 && y0 === y1) break;
    const doubled = error * 2;
    if (doubled >= dy) {
      error += dy;
      x0 += sx;
    }
    if (doubled <= dx) {
      error += dx;
      y0 += sy;
    }
  }
}

export function drawRectangle(canvas, from, to, color, size = 1, filled = false) {
  const region = normalizeRect(from, to, canvas.width, canvas.height);
  if (filled) {
    const context = canvas.getContext("2d");
    context.save();
    context.clearRect(region.x, region.y, region.width, region.height);
    if (color.a > 0) {
      context.fillStyle = `rgba(${color.r}, ${color.g}, ${color.b}, ${color.a / 255})`;
      context.fillRect(region.x, region.y, region.width, region.height);
    }
    context.restore();
    return;
  }
  const right = region.x + region.width - 1;
  const bottom = region.y + region.height - 1;
  drawLine(canvas, { x: region.x, y: region.y }, { x: right, y: region.y }, color, size);
  drawLine(canvas, { x: right, y: region.y }, { x: right, y: bottom }, color, size);
  drawLine(canvas, { x: right, y: bottom }, { x: region.x, y: bottom }, color, size);
  drawLine(canvas, { x: region.x, y: bottom }, { x: region.x, y: region.y }, color, size);
}

export function drawEllipse(canvas, from, to, color, size = 1, filled = false) {
  const region = normalizeRect(from, to, canvas.width, canvas.height);
  const centerX = region.x + (region.width - 1) / 2;
  const centerY = region.y + (region.height - 1) / 2;
  const radiusX = Math.max(0.5, (region.width - 1) / 2);
  const radiusY = Math.max(0.5, (region.height - 1) / 2);
  if (filled) {
    const context = canvas.getContext("2d");
    const image = context.getImageData(region.x, region.y, region.width, region.height);
    for (let y = region.y; y < region.y + region.height; y += 1) {
      for (let x = region.x; x < region.x + region.width; x += 1) {
        const dx = (x - centerX) / radiusX;
        const dy = (y - centerY) / radiusY;
        if (dx * dx + dy * dy <= 1) {
          const offset = ((y - region.y) * image.width + x - region.x) * 4;
          assign(image.data, offset, color);
        }
      }
    }
    context.putImageData(image, region.x, region.y);
    return;
  }
  const steps = Math.max(16, Math.ceil(Math.PI * (radiusX + radiusY) * 2));
  let previous = null;
  for (let step = 0; step <= steps; step += 1) {
    const angle = (step / steps) * Math.PI * 2;
    const point = {
      x: Math.round(centerX + Math.cos(angle) * radiusX),
      y: Math.round(centerY + Math.sin(angle) * radiusY),
    };
    if (previous !== null) drawLine(canvas, previous, point, color, size);
    previous = point;
  }
}

export function floodFill(canvas, x, y, replacement) {
  const context = canvas.getContext("2d");
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  const targetOffset = (y * canvas.width + x) * 4;
  const target = {
    r: image.data[targetOffset],
    g: image.data[targetOffset + 1],
    b: image.data[targetOffset + 2],
    a: image.data[targetOffset + 3],
  };
  if (
    target.r === replacement.r &&
    target.g === replacement.g &&
    target.b === replacement.b &&
    target.a === replacement.a
  ) {
    return false;
  }
  const stack = [[x, y]];
  const visited = new Uint8Array(canvas.width * canvas.height);
  while (stack.length > 0) {
    const [currentX, currentY] = stack.pop();
    if (
      currentX < 0 ||
      currentY < 0 ||
      currentX >= canvas.width ||
      currentY >= canvas.height
    ) {
      continue;
    }
    const index = currentY * canvas.width + currentX;
    if (visited[index]) continue;
    visited[index] = 1;
    const offset = index * 4;
    if (!matches(image.data, offset, target)) continue;
    assign(image.data, offset, replacement);
    stack.push(
      [currentX + 1, currentY],
      [currentX - 1, currentY],
      [currentX, currentY + 1],
      [currentX, currentY - 1],
    );
  }
  context.putImageData(image, 0, 0);
  return true;
}

export function replaceColor(canvas, x, y, replacement) {
  const context = canvas.getContext("2d");
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  const targetOffset = (y * canvas.width + x) * 4;
  const target = {
    r: image.data[targetOffset],
    g: image.data[targetOffset + 1],
    b: image.data[targetOffset + 2],
    a: image.data[targetOffset + 3],
  };
  let changed = false;
  for (let offset = 0; offset < image.data.length; offset += 4) {
    if (!matches(image.data, offset, target)) continue;
    assign(image.data, offset, replacement);
    changed = true;
  }
  if (changed) context.putImageData(image, 0, 0);
  return changed;
}

export function normalizeRect(from, to, width, height) {
  const left = Math.max(0, Math.min(from.x, to.x));
  const top = Math.max(0, Math.min(from.y, to.y));
  const right = Math.min(width - 1, Math.max(from.x, to.x));
  const bottom = Math.min(height - 1, Math.max(from.y, to.y));
  return {
    x: left,
    y: top,
    width: Math.max(1, right - left + 1),
    height: Math.max(1, bottom - top + 1),
  };
}

export function copyRegion(canvas, region) {
  const copy = makeCanvas(region.width, region.height);
  copy
    .getContext("2d")
    .drawImage(
      canvas,
      region.x,
      region.y,
      region.width,
      region.height,
      0,
      0,
      region.width,
      region.height,
    );
  return copy;
}

export function clearRegion(canvas, region) {
  canvas.getContext("2d").clearRect(region.x, region.y, region.width, region.height);
}

export function pasteRegion(canvas, source, x, y) {
  canvas.getContext("2d").drawImage(source, x, y);
}
