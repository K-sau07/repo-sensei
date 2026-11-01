"use client";

/**
 * The Codebase Galaxy in WebGL (6.9; React Three Fiber, loaded in the browser only): every file
 * a point of one `Points` buffer, colored by its layer; the clusters' names in the scene (buttons
 * that fly the camera there); a trace's stars joined in order; the chosen star ringed.
 *
 * Frames are drawn on demand: none while nothing moves (orbiting, a flight or the optional drift
 * asks for them). With reduced motion there is no drift and the camera jumps instead of flying.
 */
import { Line, OrbitControls } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useId, useMemo, useRef } from "react";
import * as THREE from "three";

import {
  colorBuffer,
  nearestStar,
  positionBuffer,
  viewOf,
  type GalaxyCluster,
  type GalaxyStar,
  type Rgb,
} from "@/lib/api/galaxy";

export interface GalaxyCanvasProps {
  stars: readonly GalaxyStar[];
  clusters: readonly GalaxyCluster[];
  colorOf: (layer: string) => Rgb;
  /** Lit by a search (the rest dimmed), when any. */
  lit: ReadonlySet<number> | null;
  /** A trace's stars, in its order. */
  trace: readonly number[];
  selected: number | null;
  /** The cluster to fly to (its id), and a count that asks again for the same one. */
  flight: { cluster: number; nonce: number } | null;
  drift: boolean;
  still: boolean;
  label: string;
  traceColor: string;
  onSelect: (star: number | null) => void;
  onCluster: (cluster: number) => void;
}

const CAMERA: [number, number, number] = [0, 0, 3.2];
const FLIGHT_MS = 900;
const POINT = 0.045;
const PICK_PX = 10;

type Picker = (x: number, y: number) => number | null;

/** What the flight uses of the orbit controls (drei's, made the default). */
interface Orbit {
  target: THREE.Vector3;
  update: () => void;
}

function Stars({ stars, colorOf, lit }: Pick<GalaxyCanvasProps, "stars" | "colorOf" | "lit">) {
  const invalidate = useThree((state) => state.invalidate);
  const geometry = useMemo(() => {
    const made = new THREE.BufferGeometry();
    made.setAttribute("position", new THREE.BufferAttribute(positionBuffer(stars), 3));
    made.computeBoundingSphere();
    return made;
  }, [stars]);
  useEffect(() => {
    geometry.setAttribute("color", new THREE.BufferAttribute(colorBuffer(stars, colorOf, lit), 3));
    invalidate();
  }, [geometry, stars, colorOf, lit, invalidate]);
  useEffect(
    () => () => {
      geometry.dispose();
    },
    [geometry],
  );
  const dot = useMemo(() => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 32;
    const context = canvas.getContext("2d");
    if (context) {
      context.fillStyle = "#fff";
      context.beginPath();
      context.arc(16, 16, 15, 0, Math.PI * 2);
      context.fill();
    }
    return new THREE.CanvasTexture(canvas);
  }, []);
  useEffect(
    () => () => {
      dot.dispose();
    },
    [dot],
  );
  return (
    <points geometry={geometry}>
      <pointsMaterial
        size={POINT}
        sizeAttenuation
        vertexColors
        toneMapped={false}
        map={dot}
        alphaTest={0.5}
        transparent
      />
    </points>
  );
}

/** The camera's flight to a cluster (a jump when still). */
function Flight({
  clusters,
  flight,
  still,
}: Pick<GalaxyCanvasProps, "clusters" | "flight" | "still">) {
  const camera = useThree((state) => state.camera);
  const controls = useThree((state) => state.controls) as unknown as Orbit | null;
  const invalidate = useThree((state) => state.invalidate);
  const going = useRef<{
    from: THREE.Vector3;
    to: THREE.Vector3;
    fromTarget: THREE.Vector3;
    toTarget: THREE.Vector3;
    start: number;
  } | null>(null);
  useEffect(() => {
    if (!flight || !controls) return;
    const cluster = clusters.find((c) => c.id === flight.cluster);
    if (!cluster) return;
    const view = viewOf(cluster);
    const to = new THREE.Vector3(...view.position);
    const toTarget = new THREE.Vector3(...view.target);
    if (still) {
      camera.position.copy(to);
      controls.target.copy(toTarget);
      controls.update();
      invalidate();
      return;
    }
    going.current = {
      from: camera.position.clone(),
      to,
      fromTarget: controls.target.clone(),
      toTarget,
      start: performance.now(),
    };
    invalidate();
  }, [flight, clusters, controls, camera, still, invalidate]);
  useFrame(() => {
    const now = going.current;
    if (!now || !controls) return;
    const t = Math.min(1, (performance.now() - now.start) / FLIGHT_MS);
    const eased = 1 - (1 - t) ** 3;
    camera.position.lerpVectors(now.from, now.to, eased);
    controls.target.lerpVectors(now.fromTarget, now.toTarget, eased);
    controls.update();
    if (t < 1) invalidate();
    else going.current = null;
  });
  return null;
}

/** Puts each cluster's name where its center is drawn (hidden when behind the camera). */
function LabelMover({
  clusters,
  labels,
}: {
  clusters: readonly GalaxyCluster[];
  labels: React.RefObject<(HTMLButtonElement | null)[]>;
}) {
  const at = useMemo(() => new THREE.Vector3(), []);
  useFrame(({ camera, size }) => {
    clusters.forEach((cluster, i) => {
      const element = labels.current[i];
      if (!element) return;
      at.set(cluster.x, cluster.y, cluster.z).project(camera);
      element.style.visibility = at.z > 1 ? "hidden" : "visible";
      const x = ((at.x + 1) / 2) * size.width;
      const y = ((1 - at.y) / 2) * size.height;
      element.style.transform = `translate(${String(x)}px, ${String(y)}px) translate(-50%, -50%)`;
    });
  });
  return null;
}

/** Finds the star drawn nearest a point of the canvas (CSS pixels from its top left). */
function Picking({
  stars,
  register,
}: {
  stars: readonly GalaxyStar[];
  register: (picker: Picker | null) => void;
}) {
  const camera = useThree((state) => state.camera);
  const size = useThree((state) => state.size);
  useEffect(() => {
    const at = new THREE.Vector3();
    register((x, y) =>
      nearestStar(
        stars.length,
        (i) => {
          const star = stars[i];
          if (!star) return null;
          at.set(star.x, star.y, star.z).project(camera);
          if (at.z > 1) return null; // behind the camera
          return [((at.x + 1) / 2) * size.width, ((1 - at.y) / 2) * size.height];
        },
        x,
        y,
        PICK_PX,
      ),
    );
    return () => {
      register(null);
    };
  }, [stars, camera, size, register]);
  return null;
}

export default function GalaxyCanvas({
  stars,
  clusters,
  colorOf,
  lit,
  trace,
  selected,
  flight,
  drift,
  still,
  label,
  traceColor,
  onSelect,
  onCluster,
}: GalaxyCanvasProps) {
  const picker = useRef<Picker | null>(null);
  const describedBy = useId();
  const labels = useRef<(HTMLButtonElement | null)[]>([]);
  const down = useRef<{ x: number; y: number } | null>(null);
  const tracePoints = useMemo(
    () =>
      trace.flatMap((i) => {
        const star = stars[i];
        return star ? [[star.x, star.y, star.z] as [number, number, number]] : [];
      }),
    [trace, stars],
  );
  const chosen = selected !== null ? stars[selected] : undefined;
  return (
    <div
      // Not role="img": the clusters' names in the scene are buttons.
      aria-describedby={describedBy}
      className="relative size-full"
      onPointerDown={(event) => {
        down.current = { x: event.clientX, y: event.clientY };
      }}
      onPointerUp={(event) => {
        const from = down.current;
        down.current = null;
        // A click, not the end of a drag (an orbit).
        if (!from || Math.hypot(event.clientX - from.x, event.clientY - from.y) > 4) return;
        const box = event.currentTarget.getBoundingClientRect();
        onSelect(picker.current?.(event.clientX - box.left, event.clientY - box.top) ?? null);
      }}
    >
      <p id={describedBy} className="sr-only">
        {label}
      </p>
      {/* The clusters' names, over the scene: moved to their places as frames are drawn. */}
      <ul aria-label="Clusters in view" className="pointer-events-none absolute inset-0 z-10">
        {clusters.map((cluster, i) => (
          <li key={cluster.id}>
            <button
              ref={(element) => {
                labels.current[i] = element;
              }}
              type="button"
              style={{ visibility: "hidden" }}
              className="pointer-events-auto absolute top-0 left-0 rounded-sm border border-line bg-surface-1/90 px-1.5 py-0.5 text-[11px] whitespace-nowrap text-fg shadow-sm hover:border-line-strong"
              onPointerDown={(event) => {
                event.stopPropagation();
              }}
              onPointerUp={(event) => {
                event.stopPropagation();
              }}
              onClick={() => {
                onCluster(cluster.id);
              }}
            >
              {cluster.label}
            </button>
          </li>
        ))}
      </ul>
      <Canvas
        frameloop={drift && !still ? "always" : "demand"}
        camera={{ position: CAMERA, fov: 45, near: 0.01, far: 50 }}
        dpr={[1, 2]}
        gl={{ antialias: true, powerPreference: "high-performance" }}
      >
        <Stars stars={stars} colorOf={colorOf} lit={lit} />
        {tracePoints.length > 1 ? (
          <Line points={tracePoints} color={traceColor} lineWidth={2} transparent opacity={0.9} />
        ) : null}
        {chosen ? (
          <mesh position={[chosen.x, chosen.y, chosen.z]}>
            <sphereGeometry args={[0.035, 16, 16]} />
            <meshBasicMaterial color={traceColor} wireframe />
          </mesh>
        ) : null}
        <LabelMover clusters={clusters} labels={labels} />
        <OrbitControls
          makeDefault
          enableDamping={false}
          autoRotate={drift && !still}
          autoRotateSpeed={0.4}
          minDistance={0.2}
          maxDistance={8}
        />
        <Flight clusters={clusters} flight={flight} still={still} />
        <Picking
          stars={stars}
          register={(found) => {
            picker.current = found;
          }}
        />
      </Canvas>
    </div>
  );
}
