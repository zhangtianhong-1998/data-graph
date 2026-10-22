export type Size = { width: number; height: number }
export type Camera = { zoom: number; offset: { x: number; y: number } }

/** Zoom around the visible centre, not the centre of an offscreen graph. */
export function zoomCamera(camera: Camera, zoom: number, viewport: Size): Camera {
  const ratio = zoom / camera.zoom
  return { zoom, offset: {
    x: viewport.width / 2 - (viewport.width / 2 - camera.offset.x) * ratio,
    y: viewport.height / 2 - (viewport.height / 2 - camera.offset.y) * ratio,
  } }
}

export function fitCamera(content: Size, viewport: Size): Camera {
  const zoom = Math.min(1, viewport.width / content.width, viewport.height / content.height)
  return { zoom, offset: { x: (viewport.width - content.width * zoom) / 2, y: (viewport.height - content.height * zoom) / 2 } }
}
