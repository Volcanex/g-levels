/* Capture the range's intact render geometry without a browser or store writes.
   node g_engine_mesh.mjs --repo CHECKOUT [--world range] --out FILE.mesh.json
   Imports are deliberately explicit: range/world, generator/index and building-model.
   drawBuildingModel calls drawBuildingInteriors itself; do not draw interiors twice.
   Batches are surfaces, not closed solids. Exit 2 means the engine contract changed. */
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

function argumentsOf(args) {
  const options = { world: 'range' };
  while (args.length) {
    const flag = args.shift();
    if (!['--repo', '--world', '--out'].includes(flag) || !args.length)
      throw Error(`expected --repo CHECKOUT [--world range] --out FILE.mesh.json; got ${flag}`);
    options[flag.slice(2)] = args.shift();
  }
  if (!options.repo || !options.out || options.world !== 'range')
    throw Error('--repo and --out are required; only --world range is supported');
  return options;
}

function batch(name, group) {
  return {
    name, group, material: group, closed: false, positions: [], indices: [], n: 0,
    v(vertex) {
      if (!vertex.position || vertex.position.length !== 3 || !vertex.position.every(Number.isFinite))
        throw Error(`${name}: draw vertex has no finite position`);
      this.positions.push(...vertex.position);
      this.indices.push(this.n++);
    },
    tri(a, b, c) {
      const u = b.position.map((v, i) => v - a.position[i]);
      const v = c.position.map((v, i) => v - a.position[i]);
      const cross = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]];
      if (a.normal && cross.reduce((n, x, i) => n + x * a.normal[i], 0) < 0) [b, c] = [c, b];
      for (const vertex of [a, b, c]) this.v(vertex);
    },
    quad(a, b, c, d) { this.tri(a, b, c); this.tri(a, c, d); },
  };
}

try {
  const options = argumentsOf(process.argv.slice(2));
  const load = file => import(pathToFileURL(path.resolve(options.repo, file)).href);
  const world = await load('v2/tests/range/world.mjs');
  const { generateCell } = await load('v2/sim/generator/index.mjs');
  const { drawBuildingModel, drawBuildingInteriors } = await load('v2/client/lib/building-model.mjs');
  const { signedArea, ringOf } = await load('v2/sim/generator/geometry.mjs');
  const { WALL_T } = await load('v2/sim/generator/rules.mjs');
  if (typeof drawBuildingModel !== 'function' || typeof drawBuildingInteriors !== 'function')
    throw Error('building-model must export drawBuildingModel and drawBuildingInteriors');
  const doc = world.rangeDoc();
  const generated = generateCell({ cell: world.CELL, doc, heights: world.rangeHeights() });
  const ctx = { heightAt: () => world.GROUND, roofs: doc.chunk.roofs };
  const meshes = [];
  for (const cls of generated.classes) {
    const source = cls.components.building;
    const ring = ringOf(source.record.w), sign = signedArea(ring) > 0 ? 1 : -1;
    const walls = source.walls.filter(Boolean).map(wall => {
      const a = source.record.w[wall.edge], b = source.record.w[(wall.edge + 1) % source.record.w.length];
      return { id: wall.edge, a, b, thickness: wall.t || WALL_T,
        normal: [(b[1] - a[1]) / wall.len * sign, 0, -(b[0] - a[0]) / wall.len * sign] };
    });
    const out = Object.fromEntries(['walls', 'plain', 'trim', 'lib'].map(group =>
      [group, batch(`${source.id}/${group}`, group)]));
    drawBuildingModel(out, ctx, source);
    for (const group of ['walls', 'plain', 'trim', 'lib']) {
      const { name, material, closed, positions, indices } = out[group];
      if (positions.length) meshes.push({ name, group, material, closed, positions, indices,
        building: source.id, walls });
    }
  }
  if (!meshes.length) throw Error('range generator/draw path produced no geometry');
  fs.mkdirSync(path.dirname(path.resolve(options.out)), { recursive: true });
  fs.writeFileSync(options.out, JSON.stringify({ up: 'y', ground: world.GROUND, meshes,
    source: { world: options.world, repo: path.resolve(options.repo),
      path: 'rangeDoc -> generateCell -> drawBuildingModel (includes drawBuildingInteriors)',
      winding: 'Batch.tri: cross(b-a,c-a) dot a.normal >= 0 in walls/plain/trim/lib' } }));
  console.log(`${meshes.length} meshes, ${meshes.reduce((n, m) => n + m.indices.length / 3, 0)} triangles: ${options.out}`);
} catch (error) {
  console.error(`impossible: ${error.message}`);
  process.exitCode = 2;
}
