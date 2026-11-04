"use client";

/**
 * The hero's sky in WebGL (10.3), loaded after the page is idle (see hero-sky.tsx): the
 * repository's files as stars by layer and its directory tree as faint links, a globe that turns
 * without stopping and leans toward the pointer (eased, never snapping); and the Trace Beam,
 * light that flows along the sign-in trace from the Login page to the user table without a
 * pause, its lead pulse naming each stop it passes (the labels are DOM, placed from the scene
 * each frame). Frames are drawn only while it is on screen and not paused.
 */
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";

import { useSkyColors, type Rgb } from "@/components/indexing/constellation-scene";
import { VIEW } from "@/lib/constellation";
import { SKY_LAYERS, type HeroSky } from "@/lib/demo/hero-sky";

const STAR = 0.0085;
const STOP = 0.02;
const GLOW = 3.2; // the signal color times this: over 1, so the bloom pass takes it
const DRIFT = 0.07; // rad/s: always turning
const LEAN = 0.18; // rad, at the window's edge
const EASE = 2.2; // how quickly the lean follows the pointer (per second)
const FLOW_S = 4.2; // one pulse's run from the page to the data
const PULSES = 3; // always this many on the path, evenly spaced: the flow never stops
const TRAIL = 0.14; // each pulse's lit tail, as a share of the path
const TRAIL_POINTS = 24;
const SAMPLES = 240;

function color([r, g, b]: Rgb, times = 1): THREE.Color {
  return new THREE.Color().setRGB(r * times, g * times, b * times, THREE.SRGBColorSpace);
}

/** Smooth in and out at the path's ends, so a pulse appears and fades instead of popping. */
const fade = (t: number) => Math.min(1, t / 0.08, (1 - t) / 0.08);

interface SceneProps {
  sky: HeroSky;
  labels: React.RefObject<(HTMLElement | null)[]>;
  onReady: () => void;
}

function Scene({ sky, labels, onReady }: SceneProps) {
  const colors = useSkyColors();
  const group = useRef<THREE.Group>(null);
  const stars = useRef<THREE.InstancedMesh>(null);
  const heads = useRef<(THREE.Mesh | null)[]>([]);
  const pointer = useRef({ x: 0, y: 0 });
  const { camera, size } = useThree();
  const count = sky.points.length / 3;
  const stopOf = useMemo(() => new Set(sky.beam.map((stop) => stop.star)), [sky.beam]);
  const positions = useMemo(
    () =>
      Array.from(
        { length: count },
        (_, i) =>
          new THREE.Vector3(
            sky.points[3 * i] ?? 0,
            sky.points[3 * i + 1] ?? 0,
            sky.points[3 * i + 2] ?? 0,
          ),
      ),
    [count, sky.points],
  );

  const curve = useMemo(
    () =>
      new THREE.CatmullRomCurve3(
        sky.beam.map((stop) => positions[stop.star] ?? new THREE.Vector3()),
        false,
        "centripetal",
        0.5,
      ),
    [positions, sky.beam],
  );
  // Where along the path (0..1) each stop is, to name it as the lead pulse passes.
  const reachedAt = useMemo(() => {
    const samples = curve.getSpacedPoints(SAMPLES);
    const lengths = curve.getLengths(SAMPLES);
    const total = lengths.at(-1) ?? 1;
    return sky.beam.map((stop) => {
      const target = positions[stop.star] ?? new THREE.Vector3();
      let best = 0;
      let bestDistance = Infinity;
      samples.forEach((p, i) => {
        const d = p.distanceTo(target);
        if (d < bestDistance) {
          bestDistance = d;
          best = i;
        }
      });
      return (lengths[best] ?? 0) / total;
    });
  }, [curve, positions, sky.beam]);

  const linkGeometry = useMemo(() => {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(sky.links, 3));
    return geometry;
  }, [sky.links]);
  const pathGeometry = useMemo(
    () => new THREE.BufferGeometry().setFromPoints(curve.getSpacedPoints(SAMPLES)),
    [curve],
  );
  const trails = useMemo(
    () => Array.from({ length: PULSES }, () => new THREE.BufferGeometry()),
    [],
  );
  // Mutable per frame: kept in refs (memoized values are read-only to the compiler).
  const scratch = useRef({
    vector: new THREE.Vector3(),
    tail: Array.from({ length: TRAIL_POINTS }, () => new THREE.Vector3()),
  });
  const litMaterials = useRef<THREE.LineBasicMaterial[]>([]);

  const lines = useMemo(() => {
    if (!colors) return null;
    const path = new THREE.Line(
      pathGeometry,
      new THREE.LineBasicMaterial({
        color: color(colors.signal),
        transparent: true,
        opacity: 0.3,
        toneMapped: false,
      }),
    );
    const lit = trails.map(
      (geometry) =>
        new THREE.Line(
          geometry,
          new THREE.LineBasicMaterial({
            color: color(colors.signal, GLOW),
            transparent: true,
            toneMapped: false,
          }),
        ),
    );
    return { path, lit };
  }, [colors, pathGeometry, trails]);
  useEffect(() => {
    litMaterials.current = (lines?.lit ?? []).map((line) => line.material);
    return () => {
      for (const line of [lines?.path, ...(lines?.lit ?? [])]) {
        (line?.material as THREE.Material | undefined)?.dispose();
      }
    };
  }, [lines]);

  useLayoutEffect(() => {
    const mesh = stars.current;
    if (!mesh || !colors) return;
    const matrix = new THREE.Matrix4();
    for (let i = 0; i < count; i += 1) {
      const stop = stopOf.has(i);
      const scale = stop ? STOP : STAR;
      matrix.makeScale(scale, scale, scale).setPosition(positions[i] ?? new THREE.Vector3());
      mesh.setMatrixAt(i, matrix);
      const layer = SKY_LAYERS[sky.layers[i] ?? 4] ?? "other";
      mesh.setColorAt(i, stop ? color(colors.signal, GLOW * 0.6) : color(colors.layers[layer]));
    }
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    onReady();
  }, [colors, count, onReady, positions, sky.layers, stopOf]);

  useEffect(() => {
    const move = (event: PointerEvent) => {
      pointer.current = {
        x: (event.clientX / window.innerWidth) * 2 - 1,
        y: (event.clientY / window.innerHeight) * 2 - 1,
      };
    };
    window.addEventListener("pointermove", move, { passive: true });
    return () => {
      window.removeEventListener("pointermove", move);
    };
  }, []);

  useFrame(({ clock }, delta) => {
    const g = group.current;
    if (!g) return;
    const step = Math.min(1, delta * EASE);
    g.rotation.y += DRIFT * delta;
    g.rotation.x += (VIEW.pitch + pointer.current.y * LEAN - g.rotation.x) * step;
    g.rotation.z += (-pointer.current.x * LEAN * 0.35 - g.rotation.z) * step;

    // The flow: PULSES pulses, evenly spaced, always moving from the page to the data.
    const phase = (clock.elapsedTime / FLOW_S) % 1;
    for (let p = 0; p < PULSES; p += 1) {
      const t = (phase + p / PULSES) % 1;
      const strength = fade(t);
      const head = heads.current[p];
      if (head) {
        curve.getPointAt(t, head.position);
        head.scale.setScalar(0.6 + 0.4 * strength);
      }
      const from = Math.max(0, t - TRAIL);
      const { tail } = scratch.current;
      for (let i = 0; i < TRAIL_POINTS; i += 1) {
        const point = tail[i];
        if (point) curve.getPointAt(from + ((t - from) * i) / (TRAIL_POINTS - 1), point);
      }
      trails[p]?.setFromPoints(tail);
      const material = litMaterials.current[p];
      if (material) material.opacity = strength;
    }

    // The lead pulse names the stop it last passed.
    let latest = 0;
    for (let i = 0; i < reachedAt.length; i += 1) {
      if (phase >= (reachedAt[i] ?? 1)) latest = i;
    }
    const { vector } = scratch.current;
    sky.beam.forEach((stop, i) => {
      const label = labels.current[i];
      if (!label) return;
      vector
        .copy(positions[stop.star] ?? new THREE.Vector3())
        .applyMatrix4(g.matrixWorld)
        .project(camera);
      const x = ((vector.x + 1) / 2) * size.width;
      const y = ((1 - vector.y) / 2) * size.height;
      label.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0)`;
      label.dataset.lit = i === latest ? "true" : "false";
    });
  });

  if (!colors || !lines) return null;
  return (
    <group ref={group} rotation={[VIEW.pitch, VIEW.yaw, 0]}>
      <instancedMesh ref={stars} args={[undefined, undefined, count]}>
        <sphereGeometry args={[1, 10, 10]} />
        <meshBasicMaterial toneMapped={false} />
      </instancedMesh>
      <lineSegments geometry={linkGeometry}>
        <lineBasicMaterial color={color(colors.edge)} transparent opacity={0.16} />
      </lineSegments>
      <primitive object={lines.path} />
      {lines.lit.map((line, p) => (
        <primitive key={p} object={line} />
      ))}
      {Array.from({ length: PULSES }, (_, p) => (
        <mesh
          key={p}
          ref={(node) => {
            heads.current[p] = node;
          }}
        >
          <sphereGeometry args={[0.02, 16, 16]} />
          <meshBasicMaterial color={color(colors.signal, GLOW)} toneMapped={false} />
        </mesh>
      ))}
    </group>
  );
}

export default function HeroSkyCanvas({
  sky,
  active,
  onReady,
}: {
  sky: HeroSky;
  /** On screen, the page visible and not paused: otherwise no frames at all. */
  active: boolean;
  onReady: () => void;
}) {
  const labels = useRef<(HTMLElement | null)[]>([]);
  return (
    <div className="relative size-full">
      <Canvas
        frameloop={active ? "always" : "never"}
        dpr={[1, 1.75]}
        // The globe (radius 0.78) fills about 92% of the box's height.
        camera={{ position: [0, 0, 2.05], fov: 45 }}
        gl={{ alpha: true, antialias: true, powerPreference: "high-performance" }}
        aria-hidden
      >
        <Scene sky={sky} labels={labels} onReady={onReady} />
        <EffectComposer>
          <Bloom luminanceThreshold={1} intensity={1.3} mipmapBlur radius={0.7} />
        </EffectComposer>
      </Canvas>
      <ol aria-hidden className="pointer-events-none absolute inset-0 max-lg:hidden">
        {sky.beam.map((stop, i) => (
          <li
            key={stop.star}
            ref={(node) => {
              labels.current[i] = node;
            }}
            data-lit="false"
            className="hero-stop absolute top-0 left-0 will-change-transform"
          >
            <span className="ml-3 rounded-md border border-line bg-bg/80 px-2 py-1 font-mono text-[11px] whitespace-nowrap shadow-sm backdrop-blur">
              <span className="text-signal">{stop.name}</span>{" "}
              <span className="text-fg-faint">
                {stop.path.split("/").at(-1)}
                {stop.line === null ? "" : `:${String(stop.line)}`}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
