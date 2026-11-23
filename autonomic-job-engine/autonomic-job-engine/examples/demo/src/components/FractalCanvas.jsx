import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { tileKey, tileToImageData } from "../fractal.js";

// Draws tiles as they arrive. Decoded tiles are cached so overlays can be redrawn cheaply.
const FractalCanvas = forwardRef(function FractalCanvas({ size, showWorkers, onZoom, disabled }, ref) {
  const canvasRef = useRef(null);
  const tiles = useRef(new Map()); // key -> { image, tile, color }
  const pending = useRef([]);

  const ctx = () => canvasRef.current.getContext("2d");

  function drawPending(c) {
    c.fillStyle = "#0b0c1a";
    c.fillRect(0, 0, size, size);
    c.strokeStyle = "rgba(124, 92, 255, 0.18)";
    c.lineWidth = 1;
    for (const [x, y, w, h] of pending.current) c.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  }

  function drawTile(c, entry) {
    const [x, y, w, h] = entry.tile;
    c.putImageData(entry.image, x, y);
    if (showWorkers && entry.color) {
      c.strokeStyle = entry.color;
      c.lineWidth = 2;
      c.strokeRect(x + 1, y + 1, w - 2, h - 2);
    }
  }

  function redraw() {
    const c = ctx();
    drawPending(c);
    for (const entry of tiles.current.values()) drawTile(c, entry);
  }

  useImperativeHandle(ref, () => ({
    reset(tileList) {
      tiles.current = new Map();
      pending.current = tileList;
      redraw();
    },
    addTile(result, color) {
      const key = tileKey(result.tile);
      if (tiles.current.has(key)) return false;
      const entry = { tile: result.tile, image: tileToImageData(ctx(), result), color };
      tiles.current.set(key, entry);
      drawTile(ctx(), entry);
      return true;
    },
    has: (tile) => tiles.current.has(tileKey(tile)),
  }));

  useEffect(redraw, [showWorkers, size]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleClick(e, factor) {
    e.preventDefault();
    if (disabled) return;
    const rect = canvasRef.current.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * size;
    const py = ((e.clientY - rect.top) / rect.height) * size;
    onZoom(px, py, factor);
  }

  return (
    <canvas
      ref={canvasRef}
      className={`fractal ${disabled ? "busy" : ""}`}
      width={size}
      height={size}
      title={disabled ? "rendering..." : "click: zoom in x2 and render  |  right click: zoom out"}
      onClick={(e) => handleClick(e, 0.5)}
      onContextMenu={(e) => handleClick(e, 2)}
    />
  );
});

export default FractalCanvas;
