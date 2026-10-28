"use client";

/**
 * The constellation in WebGL (React Three Fiber), loaded only in the browser (see
 * `constellation.tsx`): one `InstancedMesh` holds every star, colored by layer; one
 * `LineSegments` holds the directory links; one `Line` a trace's code edges (its stars lit).
 *
 * With live motion, a new star scales in over `--dur-slow` and glows in `--signal`, fading to
 * its layer's color over about 1.2 s; the glow is brighter than white, so it alone passes the
 * bloom pass's threshold. Links draw in after their stars. The view turns slowly while the run is
 * live. Frames are drawn on demand only: while something moves, each frame asks for the next;
 * when nothing does (or the view is off screen, or the page hidden), no frames are drawn at all.
 * With static motion (reduced motion, low-power devices) the picture is drawn as it is, without
 * animation, drift or bloom.
 */
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";

import { reach, starScale, stagger, VIEW } from "@/lib/constellation";
import { durationMs } from "@/lib/motion/tokens";

import type { Hit, Rgb, SceneProps } from "./constellation-scene";

const DRIFT = 0.015; // rad/s (at most 0.02)
const APPEAR_MS = durationMs.slow;
const GLOW_MS = 1200;
const GLOW = 3; // the signal color times this: over 1, so it blooms
const LINK_DELAY_MS = 600; // links draw in this long after their first star
const LINK_ALPHA = 0.16;
const STAR = 0.007; // the smallest star's radius, with the picture scaled to the unit sphere
const PICK_PX = 10;
const CAMERA_DISTANCE = 3;

const easeOut = (t: number) => 1 - (1 - t) ** 3;
const clamp01 = (t: number) => Math.min(1, Math.max(0, t));

function linear([r, g, b]: Rgb): THREE.Color {
  return new THREE.Color().setRGB(r, g, b, THREE.SRGBColorSpace);
}

type Picker = (x: number, y: number, width: number, height: number) => Hit | null;

/** Send every star's matrix and color to the GPU on the next frame. */
function uploadAll(target: THREE.InstancedMesh) {
  target.instanceMatrix.clearUpdateRanges();
  target.instanceMatrix.needsUpdate = true;
  if (target.instanceColor) {
    target.instanceColor.clearUpdateRanges();
    target.instanceColor.needsUpdate = true;
  }
}

interface StarsProps extends Omit<SceneProps, "label" | "onHover" | "onSelect"> {
  /** Receives the function that finds the star at a point of the canvas (null on unmount). */
  register: (picker: Picker | null) => void;
}

function Stars({
  sky,
  colors,
  motion,
  drift,
  active,
  highlight,
  trace,
  onFrame,
  register,
}: StarsProps) {
  const invalidate = useThree((state) => state.invalidate);
  const camera = useThree((state) => state.camera);
  const spin = useRef<THREE.Group>(null);
  const mesh = useRef<THREE.InstancedMesh>(null);
  const links = useRef<THREE.LineSegments>(null);
  const live = motion === "live";
  const capacity = Math.max(sky.total ?? 0, sky.points.length, 1);

  const geometry = useMemo(() => new THREE.IcosahedronGeometry(1, 0), []);
  const material = useMemo(() => new THREE.MeshBasicMaterial({ toneMapped: false }), []);
  const linkMaterial = useMemo(
    () =>
      new THREE.LineBasicMaterial({
        vertexColors: true,
        transparent: true,
        depthWrite: false,
        toneMapped: false,
      }),
    [],
  );
  useEffect(
    () => () => {
      geometry.dispose();
      material.dispose();
      linkMaterial.dispose();
    },
    [geometry, material, linkMaterial],
  );

  // Per star: position, size, layer color, when it appears; written once, animated in place.
  const state = useRef({
    generation: null as string | null,
    // False until the first fill: what is already there when the view opens is drawn as it is.
    seeded: false,
    written: 0,
    animFrom: 0,
    positions: new Float32Array(0),
    scales: new Float32Array(0),
    appear: new Float64Array(0),
    // Links: one per directory (to its parent), revealed after the first star under it.
    linkStart: new Float64Array(0),
    linksDrawing: false,
  });
  const linkPositions = useMemo(() => {
    const byId = new Map(sky.dirs.map((d) => [d.id, d]));
    const out = new Float32Array(sky.dirs.length * 6);
    sky.dirs.forEach((dir, i) => {
      const parent = dir.parent === null ? dir : (byId.get(dir.parent) ?? dir);
      out.set([dir.x, dir.y, dir.z, parent.x, parent.y, parent.z], i * 6);
    });
    return out;
  }, [sky.dirs]);

  const linkGeometry = useMemo(() => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(linkPositions, 3));
    geo.setAttribute(
      "color",
      new THREE.BufferAttribute(new Float32Array((linkPositions.length / 3) * 4), 4),
    );
    return geo;
  }, [linkPositions]);
  useEffect(
    () => () => {
      linkGeometry.dispose();
    },
    [linkGeometry],
  );

  const palette = useMemo(
    () => ({
      layers: {
        ui: linear(colors.layers.ui),
        api: linear(colors.layers.api),
        svc: linear(colors.layers.svc),
        db: linear(colors.layers.db),
        other: linear(colors.layers.other),
      },
      glow: linear(colors.signal).multiplyScalar(GLOW),
      lit: linear(colors.signal),
      edge: linear(colors.edge),
    }),
    [colors],
  );

  const scratch = useMemo(() => ({ matrix: new THREE.Matrix4(), color: new THREE.Color() }), []);

  // A trace's stars are lit; its code edges join them, in its order, in the signal color.
  const traced = useMemo(() => new Set(trace), [trace]);
  const traceLine = useMemo(() => {
    const positions = trace.flatMap((i) => {
      const point = sky.points[i];
      return point ? [point.x, point.y, point.z] : [];
    });
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(positions), 3));
    const line = new THREE.Line(
      geo,
      new THREE.LineBasicMaterial({ color: linear(colors.signal), toneMapped: false }),
    );
    line.visible = positions.length >= 6;
    return line;
  }, [trace, sky.points, colors.signal]);
  useEffect(
    () => () => {
      traceLine.geometry.dispose();
      (traceLine.material as THREE.Material).dispose();
    },
    [traceLine],
  );

  /** Star `i`'s matrix and color `now` (ms); true while it is still changing. */
  const paint = (i: number, now: number): boolean => {
    const target = mesh.current;
    const point = sky.points[i];
    if (!target || !point) return false;
    const s = state.current;
    const age = live ? now - (s.appear[i] ?? 0) : Infinity;
    const grow = live ? easeOut(clamp01(age / APPEAR_MS)) : 1;
    const glow = live ? 1 - clamp01(age / GLOW_MS) : 0;
    const lit = highlight === point.dir_cluster || traced.has(i);
    const scale = (s.scales[i] ?? 1) * STAR * grow * (lit ? 1.8 : 1);
    scratch.matrix.makeScale(scale, scale, scale);
    scratch.matrix.setPosition(
      s.positions[3 * i] ?? 0,
      s.positions[3 * i + 1] ?? 0,
      s.positions[3 * i + 2] ?? 0,
    );
    target.setMatrixAt(i, scratch.matrix);
    const base = lit ? palette.lit : palette.layers[point.layer];
    scratch.color.copy(base).lerp(palette.glow, glow);
    target.setColorAt(i, scratch.color);
    return age < GLOW_MS;
  };

  /** Every link's opacity `now`; true while one is still to draw in. */
  const paintLinks = (now: number): boolean => {
    const colorsAttr = links.current?.geometry.getAttribute("color");
    if (!(colorsAttr instanceof THREE.BufferAttribute)) return false;
    const { r, g, b } = palette.edge;
    let drawing = false;
    sky.dirs.forEach((dir, d) => {
      const start = state.current.linkStart[d] ?? Infinity;
      const shown = dir.parent !== null && start !== Infinity;
      const alpha = !shown ? 0 : live ? clamp01((now - start) / APPEAR_MS) : 1;
      if (shown && live && now - start < APPEAR_MS) drawing = true;
      colorsAttr.setXYZW(d * 2, r, g, b, alpha * LINK_ALPHA);
      colorsAttr.setXYZW(d * 2 + 1, r, g, b, alpha * LINK_ALPHA);
    });
    colorsAttr.needsUpdate = true;
    return drawing;
  };

  // New stars (and a new generation, or a new mesh, start over).
  useLayoutEffect(() => {
    const target = mesh.current;
    if (!target) return;
    const s = state.current;
    if (
      s.generation !== sky.generation ||
      s.positions.length !== capacity * 3 ||
      s.written > sky.points.length
    ) {
      s.generation = sky.generation;
      s.written = 0;
      s.animFrom = 0;
      s.positions = new Float32Array(capacity * 3);
      s.scales = new Float32Array(capacity);
      s.appear = new Float64Array(capacity);
      s.linkStart = new Float64Array(sky.dirs.length).fill(Infinity);
      target.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(capacity * 3), 3);
      (target.material as THREE.Material).needsUpdate = true; // recompiled with per-star colors
    }
    if (s.linkStart.length !== sky.dirs.length) {
      const grown = new Float64Array(sky.dirs.length).fill(Infinity);
      grown.set(s.linkStart.subarray(0, Math.min(s.linkStart.length, grown.length)));
      s.linkStart = grown;
    }
    const now = performance.now();
    const fresh = sky.points.length - s.written;
    // Only stars discovery adds while this is on screen are "just added" (scale in, glow);
    // those already there when it opens, or read after discovery finished, simply show.
    const animate = live && s.seeded && sky.status === "streaming";
    s.seeded = true;
    const delays = animate ? stagger(fresh) : [];
    const parents = new Map(sky.dirs.map((d) => [d.id, d.parent]));
    for (let k = 0; k < fresh; k += 1) {
      const i = s.written + k;
      const point = sky.points[i];
      if (!point) continue;
      s.positions.set([point.x, point.y, point.z], i * 3);
      s.scales[i] = starScale(point.size);
      s.appear[i] = animate ? now + (delays[k] ?? 0) : -Infinity;
      // Its directory and every one above it get their link, a moment after this star.
      let dir: number | null | undefined = point.dir_cluster;
      while (dir !== null && dir !== undefined && (s.linkStart[dir] ?? 0) === Infinity) {
        s.linkStart[dir] = animate ? (s.appear[i] ?? now) + LINK_DELAY_MS : -Infinity;
        dir = parents.get(dir);
      }
    }
    const from = s.written;
    s.written = sky.points.length;
    target.count = s.written;
    for (let i = from; i < s.written; i += 1) paint(i, now);
    uploadAll(target);
    s.linksDrawing = paintLinks(now);
    invalidate();
    // paint and paintLinks read the current render's values: no need to rerun for them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sky, capacity, live, invalidate, linkGeometry]);

  // A cluster lit up (or no longer), a theme change, motion turned on or off: repaint it all.
  useLayoutEffect(() => {
    const target = mesh.current;
    if (!target) return;
    const now = performance.now();
    for (let i = 0; i < state.current.written; i += 1) paint(i, now);
    uploadAll(target);
    state.current.linksDrawing = paintLinks(now);
    invalidate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlight, traced, palette, live]);

  useEffect(() => {
    if (active) invalidate(); // back on screen: draw (and carry on moving)
  }, [active, invalidate]);

  useFrame((_, delta) => {
    onFrame?.(delta * 1000);
    const target = mesh.current;
    if (!target) return;
    const s = state.current;
    const now = performance.now();
    let moving = false;
    if (live && s.animFrom < s.written) {
      let firstMoving = -1;
      for (let i = s.animFrom; i < s.written; i += 1) {
        if (paint(i, now) && firstMoving < 0) firstMoving = i;
      }
      const start = s.animFrom;
      const count = s.written - start;
      target.instanceMatrix.clearUpdateRanges();
      target.instanceMatrix.addUpdateRange(start * 16, count * 16);
      target.instanceMatrix.needsUpdate = true;
      if (target.instanceColor) {
        target.instanceColor.clearUpdateRanges();
        target.instanceColor.addUpdateRange(start * 3, count * 3);
        target.instanceColor.needsUpdate = true;
      }
      s.animFrom = firstMoving < 0 ? s.written : firstMoving;
      moving = true;
    }
    if (live && s.linksDrawing) {
      s.linksDrawing = paintLinks(now);
      moving = true;
    }
    if (live && drift && spin.current) {
      spin.current.rotation.y += DRIFT * Math.min(delta, 0.1);
      moving = true;
    }
    if (moving && active) invalidate();
  });

  // Picking: project every star with the current camera and turn, nearest to the pointer wins.
  useEffect(() => {
    register((px, py, width, height) => {
      const group = spin.current;
      const s = state.current;
      if (!group || s.written === 0) return null;
      group.updateWorldMatrix(true, false);
      const m = new THREE.Matrix4()
        .multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse)
        .multiply(group.matrixWorld);
      const e = m.elements;
      let found = -1;
      let best = PICK_PX * PICK_PX;
      let hx = 0;
      let hy = 0;
      for (let i = 0; i < s.written; i += 1) {
        const x = s.positions[3 * i] ?? 0;
        const y = s.positions[3 * i + 1] ?? 0;
        const z = s.positions[3 * i + 2] ?? 0;
        const w = e[3] * x + e[7] * y + e[11] * z + e[15];
        if (w <= 0) continue;
        const sx = (((e[0] * x + e[4] * y + e[8] * z + e[12]) / w + 1) / 2) * width;
        const sy = ((1 - (e[1] * x + e[5] * y + e[9] * z + e[13]) / w) / 2) * height;
        const distance = (sx - px) ** 2 + (sy - py) ** 2;
        if (distance <= best) {
          best = distance;
          found = i;
          hx = sx;
          hy = sy;
        }
      }
      return found < 0 ? null : { index: found, x: hx, y: hy };
    });
    return () => {
      register(null);
    };
  }, [camera, register]);

  // O(points): only when the sky changes, not on every hover.
  const scale = useMemo(() => 1 / reach(sky), [sky]);
  return (
    <group rotation={[VIEW.pitch, 0, 0]} scale={[scale, scale, scale]}>
      <group ref={spin} rotation={[0, VIEW.yaw, 0]}>
        <lineSegments ref={links} geometry={linkGeometry} material={linkMaterial} />
        <primitive object={traceLine} />
        {/* A new generation (discovery started over) gets a new mesh, so the old one's GPU
            buffers are freed with it instead of replaced on it. */}
        <instancedMesh
          key={`${String(capacity)}:${sky.generation ?? ""}`}
          ref={mesh}
          args={[geometry, material, capacity]}
          frustumCulled={false}
        />
      </group>
    </group>
  );
}

/** The WebGL constellation. Default export, for `next/dynamic`. */
export default function ConstellationCanvas(props: SceneProps) {
  const { label, onHover, onSelect, colors, motion, active } = props;
  const box = useRef<HTMLDivElement>(null);
  const picker = useRef<Picker | null>(null);
  const register = useCallback((found: Picker | null) => {
    picker.current = found;
  }, []);
  const down = useRef<{ x: number; y: number } | null>(null);
  const pending = useRef<number | null>(null);
  const background = useMemo(() => linear(colors.background), [colors.background]);

  const pick = (event: { clientX: number; clientY: number }): Hit | null => {
    const rect = box.current?.getBoundingClientRect();
    if (!rect || !picker.current) return null;
    return picker.current(
      event.clientX - rect.left,
      event.clientY - rect.top,
      rect.width,
      rect.height,
    );
  };

  useEffect(
    () => () => {
      if (pending.current !== null) cancelAnimationFrame(pending.current);
    },
    [],
  );

  return (
    <div
      ref={box}
      role="img"
      aria-label={label}
      className="size-full touch-none"
      onPointerMove={(event) => {
        const at = { clientX: event.clientX, clientY: event.clientY };
        if (pending.current !== null) return; // once per frame at most
        pending.current = requestAnimationFrame(() => {
          pending.current = null;
          onHover(pick(at));
        });
      }}
      onPointerLeave={() => {
        onHover(null);
      }}
      onPointerDown={(event) => {
        down.current = { x: event.clientX, y: event.clientY };
      }}
      onPointerUp={(event) => {
        // A click, not the end of a drag.
        const start = down.current;
        down.current = null;
        if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 5) {
          onSelect(pick(event));
        }
      }}
      data-testid="constellation-webgl"
    >
      <Canvas
        frameloop={active ? "demand" : "never"}
        dpr={[1, 2]}
        camera={{ position: [0, 0, CAMERA_DISTANCE], fov: 40, near: 0.1, far: 20 }}
        gl={{ antialias: true, powerPreference: "high-performance" }}
        aria-hidden
      >
        <color attach="background" args={[background]} />
        <Stars {...props} register={register} />
        {motion === "live" ? (
          <EffectComposer multisampling={4}>
            <Bloom luminanceThreshold={1} luminanceSmoothing={0.05} intensity={0.8} mipmapBlur />
          </EffectComposer>
        ) : null}
      </Canvas>
    </div>
  );
}
