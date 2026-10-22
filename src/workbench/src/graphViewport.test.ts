import { describe, expect, it } from 'vitest'
import { fitCamera, zoomCamera } from './graphViewport'

describe('graph camera', () => {
  it('fits tall graphs entirely inside a short viewport', () => {
    const content = { width: 600, height: 1500 }
    const viewport = { width: 600, height: 400 }
    const camera = fitCamera(content, viewport)
    expect(camera.offset.x).toBeGreaterThanOrEqual(0)
    expect(camera.offset.y).toBeGreaterThanOrEqual(0)
    expect(camera.offset.x + content.width * camera.zoom).toBeLessThanOrEqual(viewport.width)
    expect(camera.offset.y + content.height * camera.zoom).toBeLessThanOrEqual(viewport.height)
  })
  it('keeps the inspected point at the viewport centre while zooming', () => {
    const viewport = { width: 600, height: 400 }
    const before = { zoom: .4, offset: { x: -20, y: -500 } }
    const point = { x: (300 - before.offset.x) / before.zoom, y: (200 - before.offset.y) / before.zoom }
    const after = zoomCamera(before, .8, viewport)
    expect(point.x * after.zoom + after.offset.x).toBeCloseTo(300)
    expect(point.y * after.zoom + after.offset.y).toBeCloseTo(200)
  })
})
