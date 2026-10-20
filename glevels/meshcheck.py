"""Geometry half of correct.render.frame-integrity; no renderer required.

    python3 glevels/meshcheck.py [--out DIR] [--json] [--ground Y] FILE...

Inputs: OBJ polygons (fan triangulated), glTF/GLB triangle primitives with node
transforms, .mesh.json surfaces (positions, indices, group, material, closed;
optional building and wall-plane metadata),
or .vox.json (size, integer voxels, anchored: "min-y" or anchor coordinates).
Mesh JSON uses Y up; OBJ/glTF default to ground Y=0. --ground overrides it.
Only static, uncompressed glTF is supported; skins/morphs are reported impossible.

Writes findings.json with counts, groups and sampled coordinates into --out
(default a printed temporary directory). Prints blocking faults and reviewer
leads; --json prints the same structured findings instead. Exit 0: no clear
faults; 1: blocking faults; 2: impossible (missing numpy, invalid/unsupported
input or insufficient resources). Leads alone never fail a run.
"""
import argparse
import base64
from collections import defaultdict
import itertools
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import time
from urllib.parse import unquote

REL_EPS = 1e-7  # Position tolerance relative to the scene's bounding diagonal.
NORMAL_EPS = 1e-6  # Parallel normals may differ by this much in their dot product.
SLIVER = 1e-7  # Ignore overlap below this fraction of the smaller triangle's area.
BLOCKING_COUNT = 3  # A few coplanar overlaps remain leads; repeated hits block.
SAMPLES = 8  # Keep the worst coordinates per check/group, while counting every hit.


def numpy():
    global np
    import numpy as np
    return np


def mesh(name, positions, indices, group=None, material='', closed=False, building=None, walls=()):
    positions = np.asarray(positions, dtype=float).reshape(-1, 3)
    raw = np.asarray(indices)
    indices = raw.astype(np.int64).reshape(-1, 3)
    if not np.isfinite(positions).all() or not np.array_equal(raw.reshape(-1), indices.reshape(-1)):
        raise ValueError(f'{name}: non-finite positions or non-integer indices')
    if indices.size and (indices.min() < 0 or indices.max() >= len(positions)):
        raise ValueError(f'{name}: index outside positions')
    return dict(name=name, positions=positions, indices=indices,
                group=group or name, material=str(material), closed=bool(closed),
                building=building, walls=walls)


def obj(path):
    positions, faces = [], defaultdict(list)
    group, material = 'default', ''
    for line in path.read_text().splitlines():
        fields = line.split('#', 1)[0].split()
        if not fields:
            continue
        if fields[0] == 'v':
            positions.append([float(x) for x in fields[1:4]])
        elif fields[0] in ('g', 'o'):
            group = ' '.join(fields[1:]) or 'default'
        elif fields[0] == 'usemtl':
            material = ' '.join(fields[1:])
        elif fields[0] == 'f':
            ids = [int(x.split('/')[0]) for x in fields[1:]]
            if any(i == 0 for i in ids) or len(ids) < 3:
                raise ValueError('OBJ face requires at least three nonzero indices')
            ids = [i - 1 if i > 0 else len(positions) + i for i in ids]
            for k in range(1, len(ids) - 1):
                faces[group, material].append([ids[0], ids[k], ids[k + 1]])
    return [mesh(f'{path.name}/{g}/{m}', positions, ids, g, m) for (g, m), ids in faces.items()]


def gltf(path):
    binary = None
    if path.suffix.lower() == '.glb':
        data = path.read_bytes()
        magic, version, length = struct.unpack_from('<4sII', data)
        if magic != b'glTF' or version != 2 or length != len(data):
            raise ValueError('invalid GLB v2 header')
        offset, doc = 12, None
        while offset < len(data):
            size, kind = struct.unpack_from('<II', data, offset)
            chunk = data[offset + 8:offset + 8 + size]
            if len(chunk) != size:
                raise ValueError('truncated GLB chunk')
            if kind == 0x4e4f534a:
                doc = json.loads(chunk)
            elif kind == 0x004e4942:
                binary = chunk
            offset += size + 8
        if doc is None:
            raise ValueError('GLB has no JSON chunk')
    else:
        doc = json.loads(path.read_text())
    if str(doc.get('asset', {}).get('version', '')).split('.')[0] != '2':
        raise ValueError('only glTF 2 is supported')
    if doc.get('extensionsRequired'):
        raise ValueError(f'unsupported required glTF extensions: {doc["extensionsRequired"]}')
    buffers = []
    for b in doc.get('buffers', []):
        uri = b.get('uri')
        if uri is None:
            data = binary
        elif uri.startswith('data:'):
            header, body = uri.split(',', 1)
            data = base64.b64decode(body) if ';base64' in header else unquote(body).encode('latin1')
        elif '://' in uri:
            raise ValueError('remote glTF buffers are unsupported; use local files')
        else:
            data = (path.parent / unquote(uri)).read_bytes()
        if data is None or len(data) < b['byteLength']:
            raise ValueError('missing or truncated glTF buffer')
        buffers.append(data)
    dtypes = {5120: 'i1', 5121: 'u1', 5122: '<i2', 5123: '<u2', 5125: '<u4', 5126: '<f4'}

    def read(view, offset, count, width, dtype):
        v = doc['bufferViews'][view]
        dtype = np.dtype(dtype)
        stride = v.get('byteStride', width * dtype.itemsize)
        needed = offset + max(0, count - 1) * stride + (width * dtype.itemsize if count else 0)
        if needed > v['byteLength']:
            raise ValueError('glTF accessor exceeds bufferView')
        return np.ndarray((count, width), dtype=dtype, buffer=buffers[v['buffer']],
                          offset=v.get('byteOffset', 0) + offset,
                          strides=(stride, dtype.itemsize)).copy()

    def accessor(index):
        a = doc['accessors'][index]
        width = {'SCALAR': 1, 'VEC3': 3}[a['type']]
        dtype = dtypes[a['componentType']]
        values = read(a['bufferView'], a.get('byteOffset', 0), a['count'], width, dtype) if 'bufferView' in a else np.zeros((a['count'], width), dtype=dtype)
        if 'sparse' in a:
            s = a['sparse']
            ids = read(s['indices']['bufferView'], s['indices'].get('byteOffset', 0), s['count'], 1, dtypes[s['indices']['componentType']]).ravel()
            values[ids] = read(s['values']['bufferView'], s['values'].get('byteOffset', 0), s['count'], width, dtype)
        if a.get('normalized') and a['componentType'] != 5126:
            info = np.iinfo(values.dtype)
            values = np.maximum(values.astype(float) / info.max, -1)
        return values

    def transform(node):
        if 'matrix' in node:
            return np.array(node['matrix'], dtype=float).reshape(4, 4).T
        x, y, z, w = node.get('rotation', [0, 0, 0, 1])
        rot = np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])
        out = np.eye(4)
        out[:3, :3] = rot @ np.diag(node.get('scale', [1, 1, 1]))
        out[:3, 3] = node.get('translation', [0, 0, 0])
        return out

    out = []

    def visit(index, parent, ancestry):
        if index in ancestry:
            raise ValueError('cyclic glTF nodes')
        node = doc['nodes'][index]
        if 'skin' in node or node.get('weights'):
            raise ValueError('skinned/morphed glTF requires a baked static mesh')
        world = parent @ transform(node)
        if 'mesh' in node:
            model = doc['meshes'][node['mesh']]
            for k, p in enumerate(model['primitives']):
                if p.get('mode', 4) != 4 or p.get('targets'):
                    raise ValueError('glTF requires static TRIANGLES primitives')
                xyz = accessor(p['attributes']['POSITION'])
                xyz = xyz @ world[:3, :3].T + world[:3, 3]
                ids = accessor(p['indices']).ravel() if 'indices' in p else np.arange(len(xyz))
                name = f'{node.get("name", index)}/{model.get("name", node["mesh"])}/{k}'
                extra = {**model.get('extras', {}), **node.get('extras', {})}
                out.append(mesh(name, xyz, ids, extra.get('group', str(index)),
                                p.get('material', ''), extra.get('closed', False)))
        for child in node.get('children', []):
            visit(child, world, ancestry | {index})

    nodes = doc.get('nodes', [])
    if doc.get('scenes'):
        roots = doc['scenes'][doc.get('scene', 0)].get('nodes', [])
    else:
        children = {c for n in nodes for c in n.get('children', [])}
        roots = [i for i in range(len(nodes)) if i not in children]
    for root in roots:
        visit(root, np.eye(4), set())
    return out


class Findings:
    def __init__(self):
        self.rows = {}

    def add(self, check, group, location, detail, weight=0, count=1, severity='review', where=(), **extra):
        key = check, group, severity
        row = self.rows.setdefault(key, dict(check=check, group=group, severity=severity,
                                           count=0, samples=[], measure=0.0, by_location={}, by_building={}))
        row['count'] += count
        row['measure'] += weight
        for building in {p.rsplit('/wall-', 1)[0] for p in where}:
            row['by_building'][building] = row['by_building'].get(building, 0) + count
        for place in sorted(set(where)):
            row['by_location'][place] = row['by_location'].get(place, 0) + count
        row['samples'].append(dict(location=[float(v) for v in location], detail=detail,
                                   weight=float(weight), where=sorted(set(where)), **extra))
        row['samples'].sort(key=lambda s: -s['weight'])
        del row['samples'][SAMPLES:]

    def finish(self):
        counts = defaultdict(int)
        for row in self.rows.values():
            counts[row['check']] += row['count']
        counts['t-junction'] = counts['t-junction-crack'] + counts['t-junction-fan']
        for row in self.rows.values():
            if row['check'] == 'z-fight' and counts['z-fight'] > BLOCKING_COUNT:
                row['severity'] = 'block'
        return dict(counts), sorted(self.rows.values(), key=lambda r: (r['severity'], r['check'], r['group']))


class Union:
    def __init__(self, n):
        self.parent = list(range(n))

    def root(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def join(self, a, b):
        a, b = self.root(int(a)), self.root(int(b))
        if a != b:
            self.parent[b] = a


def weld(points, eps):
    unique, inverse = np.unique(points, axis=0, return_inverse=True)
    cells, union = defaultdict(list), Union(len(unique))
    offsets = list(itertools.product((-1, 0, 1), repeat=3))
    origin = unique[0] if len(unique) else np.zeros(3)
    for i, p in enumerate(unique):
        key = tuple(np.floor((p - origin) / eps).astype(np.int64))
        for delta in offsets:
            for j in cells.get(tuple(a + b for a, b in zip(key, delta)), ()):
                if np.dot(p - unique[j], p - unique[j]) <= eps * eps:
                    union.join(i, j)
        cells[key].append(i)
    roots = np.array([union.root(i) for i in range(len(unique))])
    return unique, roots[inverse]


class Boxes:
    def __init__(self, low, high, eps):
        self.low, self.high, self.eps = low, high, eps
        lengths = np.max(high - low, axis=1)
        self.base = max(float(np.median(lengths)), eps * 4)
        self.level = np.maximum(0, np.ceil(np.log2(np.maximum(lengths, self.base) / self.base))).astype(int)
        self.grids = {}
        for level in sorted(set(self.level.tolist())):
            step = self.base * 2 ** level
            grid = defaultdict(list)
            for i in np.flatnonzero(self.level == level):
                a, b = np.floor(low[i] / step).astype(int), np.floor(high[i] / step).astype(int)
                for key in itertools.product(*(range(x, y + 1) for x, y in zip(a, b))):
                    grid[key].append(int(i))
            self.grids[level] = grid

    def query(self, low, high, level=0):
        found = set()
        for lev, grid in self.grids.items():
            if lev < level:
                continue
            step = self.base * 2 ** lev
            a = np.floor((low - self.eps) / step).astype(int)
            b = np.floor((high + self.eps) / step).astype(int)
            for key in itertools.product(*(range(x, y + 1) for x, y in zip(a, b))):
                found.update(grid.get(key, ()))
        return np.fromiter(found, dtype=np.int64)

    def pairs(self):
        for i in range(len(self.low)):
            ids = self.query(self.low[i], self.high[i], self.level[i])
            ids = ids[(self.level[ids] > self.level[i]) | (ids > i)]
            if len(ids):
                mask = np.all(self.low[ids] <= self.high[i] + self.eps, axis=1) & np.all(self.high[ids] >= self.low[i] - self.eps, axis=1)
                yield i, ids[mask]

    def batches(self):
        left, right, count = [], [], 0
        for i, ids in self.pairs():
            if not len(ids):
                continue
            left.append(np.full(len(ids), i, dtype=np.int64))
            right.append(ids)
            count += len(ids)
            if count >= 16384:
                yield np.concatenate(left), np.concatenate(right)
                left, right, count = [], [], 0
        if count:
            yield np.concatenate(left), np.concatenate(right)


def cross2(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def overlap(a, b, normal):
    origin = a[0].copy()
    a, b = a - origin, b - origin
    drop = int(np.argmax(np.abs(normal)))
    axes = [i for i in range(3) if i != drop]
    polygon = list(a[:, axes])
    clip = b[:, axes]
    sign = 1 if cross2(clip[1] - clip[0], clip[2] - clip[0]) > 0 else -1
    for j in range(3):
        p, q = clip[j], clip[(j + 1) % 3]
        result = []
        for k, v in enumerate(polygon):
            prev = polygon[k - 1]
            d0, d1 = sign * cross2(q - p, prev - p), sign * cross2(q - p, v - p)
            if (d0 >= 0) != (d1 >= 0):
                result.append(prev + (v - prev) * d0 / (d0 - d1))
            if d1 >= 0:
                result.append(v)
        polygon = result
        if not polygon:
            return 0, None
    polygon = np.array(polygon)
    area = abs(float(np.sum(cross2(polygon, np.roll(polygon, -1, axis=0))))) / (2 * abs(normal[drop]))
    point = np.zeros(3)
    point[axes] = polygon.mean(axis=0)
    point[drop] = (np.dot(normal, a[0]) - np.dot(normal[axes], point[axes])) / normal[drop]
    return area, point + origin


def segment_in_face(a, b, face, normal, eps):
    drop = int(np.argmax(np.abs(normal)))
    axes = [i for i in range(3) if i != drop]
    tri = face[:, axes]
    sign = 1 if cross2(tri[1] - tri[0], tri[2] - tri[0]) > 0 else -1
    lo, hi = 0., 1.
    for k in range(3):
        edge = tri[(k + 1) % 3] - tri[k]
        margin = eps * np.linalg.norm(edge)
        x = sign * cross2(edge, a[axes] - tri[k]) - margin
        y = sign * cross2(edge, b[axes] - tri[k]) - margin
        if x <= 0 and y <= 0:
            return 0, None
        if x < 0:
            lo = max(lo, x / (x - y))
        if y < 0:
            hi = min(hi, x / (x - y))
    length = max(0, hi - lo) * float(np.linalg.norm(b - a))
    return length, a + (b - a) * ((lo + hi) / 2)


def point_on_faces(point, faces, normals, eps):
    distance = np.einsum('ij,ij->i', point - faces[:, 0], normals)
    projected = point - distance[:, None] * normals
    inside = np.abs(distance) <= eps
    for k in range(3):
        edge = faces[:, (k + 1) % 3] - faces[:, k]
        side = np.einsum('ij,ij->i', np.cross(edge, projected - faces[:, k]), normals)
        inside &= side >= -eps * np.linalg.norm(edge, axis=1)
    return inside


def front_visible(point, normal, triangles, normals, eps):
    # An unobstructed ray along A's normal witnesses an exterior view of its front.
    denominator = normals @ normal
    ids = np.flatnonzero(np.abs(denominator) > NORMAL_EPS)
    distance = np.einsum('ij,ij->i', triangles[ids, 0] - point, normals[ids]) / denominator[ids]
    ahead = distance > eps * 2
    ids, distance = ids[ahead], distance[ahead]
    hit = point + distance[:, None] * normal
    inside = np.ones(len(ids), dtype=bool)
    for k in range(3):
        edge = triangles[ids, (k + 1) % 3] - triangles[ids, k]
        side = np.einsum('ij,ij->i', np.cross(edge, hit - triangles[ids, k]), normals[ids])
        inside &= side >= -eps * np.linalg.norm(edge, axis=1)
    return not np.any(inside)


def inspect_meshes(meshes, ground, findings):
    triangles, owners = [], []
    for i, m in enumerate(meshes):
        triangles.append(m['positions'][m['indices']])
        owners.extend([i] * len(m['indices']))
    tri, owners = np.concatenate(triangles), np.array(owners)
    if not len(tri):
        raise ValueError('input has no triangles')
    scale = float(np.linalg.norm(np.ptp(tri.reshape(-1, 3), axis=0)))
    eps = max(scale * REL_EPS, 1e-10)
    points, vertices = weld(tri.reshape(-1, 3), eps)
    vertices = vertices.reshape(-1, 3)
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    twice = np.linalg.norm(normals, axis=1)
    lengths = np.max(np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2), axis=1)
    good = twice > eps * lengths
    normals[good] /= twice[good, None]

    def whereabouts(ids, point):
        places = set()
        for owner in set(int(i) for i in ids):
            m = meshes[owner]
            if m['building'] is None:
                continue
            matched = False
            for wall in m['walls']:
                a, b = np.array(wall['a']), np.array(wall['b'])
                p = np.asarray(point)[[0, 2]] - a
                direction = b - a
                along = np.dot(p, direction) / np.dot(direction, direction)
                depth = np.dot(p, np.array(wall['normal'])[[0, 2]])
                margin = eps / np.linalg.norm(direction)
                if -margin <= along <= 1 + margin and -wall['thickness'] - eps <= depth <= eps:
                    places.add(f'{m["building"]}/wall-{wall["id"]}')
                    matched = True
            if not matched:
                places.add(str(m['building']))
        return sorted(places)

    degenerate_vertices = defaultdict(list)
    for i in np.flatnonzero(~good):
        for v in set(vertices[i]):
            degenerate_vertices[int(owners[i]), int(v)].append(int(i))
        point = tri[i].mean(axis=0)
        findings.add('degenerate', meshes[owners[i]]['group'], point, 'zero or nearly zero triangle area',
                     triangle=int(i), mesh=meshes[owners[i]]['name'], where=whereabouts([owners[i]], point))
    all_tri, all_vertices = tri, vertices
    original = np.flatnonzero(good)
    tri, owners, vertices, normals, twice = (a[good] for a in (tri, owners, vertices, normals, twice))
    if not len(tri):
        return dict(triangles=len(good), epsilon=eps, components=0)
    groups = np.array([m['group'] for m in meshes])[owners]
    surfaces = [(m['group'], m['material']) for m in meshes]
    surface_map = {s: i for i, s in enumerate(surfaces)}
    surface_ids = np.array([surface_map[s] for s in surfaces])[owners]
    low, high = tri.min(axis=1), tri.max(axis=1)
    boxes = Boxes(low, high, eps)
    seen_t, seen_edge = set(), set()

    def evidence(a, b):
        return dict(triangles=[int(original[a]), int(original[b])],
                    meshes=[meshes[owners[a]]['name'], meshes[owners[b]]['name']])

    def junctions(targets, sources):
        shared = np.sum(np.any(vertices[targets, :, None] == vertices[sources, None, :], axis=2), axis=1)
        targets, sources = targets[shared < 2], sources[shared < 2]
        if not len(sources):
            return
        a = tri[targets]
        edges = np.roll(a, -1, axis=1) - a
        length2 = np.einsum('nej,nej->ne', edges, edges)
        rel = tri[sources, :, None, :] - a[:, None, :, :]
        t = np.einsum('nvej,nej->nve', rel, edges) / length2[:, None, :]
        off = np.linalg.norm(rel - t[..., None] * edges[:, None, :, :], axis=3)
        lengths = np.sqrt(length2)[:, None, :]
        hit = (off <= eps) & (t * lengths > eps) & ((1 - t) * lengths > eps)
        for si, v, e in np.argwhere(hit):
            source, target = sources[si], targets[si]
            edge, face = edges[si, e], a[si]
            side = np.dot(np.cross(edge, face[(e + 2) % 3] - face[e]), normals[target])
            other = np.cross(edge, tri[source] - face[e]) @ normals[target]
            if not np.any(other * np.sign(side) < -eps * math.sqrt(length2[si, e])):
                continue
            key = tuple(sorted((int(vertices[target, e]), int(vertices[target, (e + 1) % 3])))) + (int(vertices[source, v]),)
            if key in seen_t:
                continue
            seen_t.add(key)
            depth = min(t[si, v, e], 1 - t[si, v, e]) * math.sqrt(length2[si, e])
            fan = []
            for d in degenerate_vertices.get((int(owners[target]), int(vertices[source, v])), ()):
                corners = set(all_vertices[d])
                if len(corners) < 3 or not corners.intersection(vertices[target, [e, (e + 1) % 3]]):
                    continue
                rel = all_tri[d] - face[e]
                along = rel @ edge / length2[si, e]
                off = np.linalg.norm(rel - along[:, None] * edge, axis=1)
                margin = eps / math.sqrt(length2[si, e])
                if np.all(off <= eps) and np.all(along >= -margin) and np.all(along <= 1 + margin):
                    fan.append(d)
            cause = ('fan from a collinear corner: triangulate from a non-collinear corner or ear-clip'
                     if fan else 'vertex strictly inside an adjacent coplanar edge: missing joinUp between pieces/faces')
            point = tri[source, v]
            findings.add('t-junction-fan' if fan else 't-junction-crack', groups[target], point, cause,
                         severity='block', weight=depth, degenerate_triangles=fan,
                         where=whereabouts(owners[[target, source]], point),
                         edge=face[[e, (e + 1) % 3]].tolist(), **evidence(target, source))

    for left, right in boxes.batches():
        parallel = np.abs(np.einsum('ij,ij->i', normals[left], normals[right])) > 1 - NORMAL_EPS
        plane = np.max(np.abs(np.einsum('ijk,ik->ij', tri[right] - tri[left, 0, None], normals[left])), axis=1) <= eps
        coplanar = parallel & plane
        junctions(left[coplanar], right[coplanar])
        junctions(right[coplanar], left[coplanar])
        different = surface_ids[left] != surface_ids[right]
        for i, j in zip(left[coplanar & different], right[coplanar & different]):
            area, point = overlap(tri[i], tri[j], normals[i])
            minimum = min(twice[i], twice[j]) / 2
            if area > max(minimum * SLIVER, eps * eps):
                findings.add('z-fight', f'{groups[i]} / {groups[j]}', point, 'coplanar projected overlap', weight=area,
                             area=area, where=whereabouts(owners[[i, j]], point), **evidence(i, j))
        for i, j in zip(left[~parallel], right[~parallel]):
            for source, target in ((i, j), (j, i)):
                signed = (tri[source] - tri[target, 0]) @ normals[target]
                if signed.mean() >= -eps:
                    continue
                distance = np.abs(signed)
                for e in range(3):
                    end = (e + 1) % 3
                    if max(distance[e], distance[end]) > eps:
                        continue
                    length, point = segment_in_face(tri[source, e], tri[source, end], tri[target], normals[target], eps)
                    key = (tuple(sorted((int(vertices[source, e]), int(vertices[source, end])))), int(target))
                    if length > eps * 2 and key not in seen_edge and front_visible(point, normals[target], tri, normals, eps):
                        seen_edge.add(key)
                        findings.add('bleed-through', f'{groups[source]} / {groups[target]}', point,
                                     'back-side face edge reaches the interior of a visible front surface',
                                     severity='block', weight=length, length=length,
                                     centroid_depth=float(signed.mean()), front_normal=normals[target].tolist(),
                                     edge=tri[source, [e, end]].tolist(), faces=[tri[source].tolist(), tri[target].tolist()],
                                     where=whereabouts(owners[[source, target]], point), **evidence(source, target))

    components = Union(len(points))
    for a, b, c in vertices:
        components.join(a, b)
        components.join(a, c)
    roots = np.array([components.root(int(v)) for v in vertices[:, 0]])
    component_ids = sorted(set(roots.tolist()))
    network = Union(len(points))
    vertex_faces = defaultdict(list)
    for i, row in enumerate(vertices):
        for v in row:
            vertex_faces[int(v)].append(i)
    contacts = defaultdict(set)
    for vertex, faces in vertex_faces.items() if len(component_ids) > 1 else ():
        p = points[vertex]
        candidates = boxes.query(p, p)
        root = roots[faces[0]]
        candidates = candidates[roots[candidates] != root]
        if len(candidates):
            for other in candidates[point_on_faces(p, tri[candidates], normals[candidates], eps)]:
                target = roots[other]
                network.join(root, target)
                contacts[tuple(sorted((int(root), int(target))))].add(vertex)
    anchored = {network.root(int(roots[i])) for i in np.flatnonzero(low[:, 1] <= ground + eps)}
    floating = defaultdict(list)
    for i in component_ids:
        if network.root(i) not in anchored:
            floating[network.root(i)].append(i)
    for comp in floating.values():
        ids = np.flatnonzero(np.isin(roots, comp))
        findings.add('floating', ' / '.join(sorted(set(groups[ids]))), tri[ids].mean(axis=(0, 1)),
                     f'{len(ids)} triangles do not reach ground Y={ground:g} through vertex/face contacts', severity='block',
                     where=whereabouts(owners[ids], tri[ids].mean(axis=(0, 1))))
    for (a, b), shared in contacts.items():
        xyz = points[list(shared)]
        rank = np.linalg.matrix_rank(xyz - xyz[0], tol=eps)
        if rank < 2:
            findings.add('attachment', ' / '.join(sorted(set(groups[np.isin(roots, [a, b])]))), xyz.mean(axis=0),
                         'components touch only at one vertex' if rank == 0 else 'components touch only along one edge',
                         where=whereabouts(owners[np.isin(roots, [a, b])], xyz.mean(axis=0)))

    edges = np.sort(np.concatenate((vertices[:, [0, 1]], vertices[:, [1, 2]], vertices[:, [2, 0]])), axis=1)
    face_ids = np.tile(np.arange(len(tri)), 3)
    unique_edges, inverse, counts = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
    patches = Union(len(tri))
    order = np.argsort(inverse, kind='stable')
    starts = np.r_[0, np.cumsum(counts)]
    for edge, count in enumerate(counts):
        ids = face_ids[order[starts[edge]:starts[edge + 1]]]
        if count == 2:
            patches.join(*ids)
        if count > 2:
            point = points[unique_edges[edge]].mean(axis=0)
            findings.add('non-manifold', ' / '.join(sorted(set(groups[ids]))), point, f'{count} faces share an edge',
                         where=whereabouts(owners[ids], point))
    for m_id, m in enumerate(meshes):
        if not m['closed']:
            continue
        subset = edges[owners[face_ids] == m_id]
        ue, nc = np.unique(subset, axis=0, return_counts=True)
        for edge in ue[nc == 1]:
            findings.add('open-edge', m['group'], points[edge].mean(axis=0), f'{m["name"]}: boundary edge on declared-closed mesh',
                         severity='block', edge=points[edge].tolist(), where=whereabouts([m_id], points[edge].mean(axis=0)))
    patch_roots = np.array([patches.root(i) for i in range(len(tri))])
    weak = defaultdict(set)
    for vertex, faces in vertex_faces.items():
        parts = sorted(set(patch_roots[faces].tolist()))
        for a, b in itertools.combinations(parts, 2):
            weak[a, b].add(vertex)
    for (a, b), shared in weak.items():
        xyz = points[list(shared)]
        rank = np.linalg.matrix_rank(xyz - xyz[0], tol=eps)
        if rank < 2:
            findings.add('attachment', ' / '.join(sorted(set(groups[[a, b]]))), xyz.mean(axis=0),
                         'surface patches meet only at one vertex' if rank == 0 else 'surface patches meet only along one edge',
                         where=whereabouts(owners[[a, b]], xyz.mean(axis=0)))
    return dict(triangles=len(good), epsilon=eps, components=len(component_ids))


def inspect_voxels(doc, findings):
    size = float(doc['size'])
    raw = np.asarray(doc['voxels'])
    xyz = raw.astype(np.int64).reshape(-1, 3)
    if not np.array_equal(raw.reshape(-1), xyz.ravel()) or not math.isfinite(size) or size <= 0 or not len(xyz):
        raise ValueError('voxels require integer coordinates and a positive finite size')
    voxels = set(map(tuple, xyz.tolist()))
    anchor = doc.get('anchored', 'min-y')
    minimum = min(v[1] for v in voxels)
    if anchor == 'min-y':
        anchors = {v for v in voxels if v[1] == minimum}
    else:
        raw = np.asarray(anchor)
        values = raw.astype(np.int64).reshape(-1, 3)
        if not np.array_equal(raw.ravel(), values.ravel()):
            raise ValueError('voxel anchors require integer coordinates')
        anchors = set(map(tuple, values.tolist())) & voxels
    six = [d for d in itertools.product((-1, 0, 1), repeat=3) if sum(abs(x) for x in d) == 1]
    diagonal = [d for d in itertools.product((-1, 0, 1), repeat=3) if sum(abs(x) for x in d) > 1]
    adjacency = {v: [tuple(a + b for a, b in zip(v, d)) for d in six if tuple(a + b for a, b in zip(v, d)) in voxels] for v in voxels}
    components, labels = [], {}
    for start in sorted(voxels):
        if start in labels:
            continue
        todo, part = [start], []
        labels[start] = len(components)
        while todo:
            v = todo.pop()
            part.append(v)
            for n in adjacency[v]:
                if n not in labels:
                    labels[n] = len(components)
                    todo.append(n)
        components.append(part)
    rooted = {labels[v] for v in anchors}
    anchor_counts = [sum(v in anchors for v in part) for part in components]
    for i, part in enumerate(components):
        if i in rooted:
            continue
        location = (np.mean(part, axis=0) + .5) * size
        findings.add('floating', 'voxels', location, f'{len(part)} voxels do not reach an anchor by faces', severity='block')
        links = set()
        for v in part:
            for d in diagonal:
                n = tuple(a + b for a, b in zip(v, d))
                if n in labels and labels[n] != i:
                    links.add(n)
        if links:
            findings.add('attachment', 'voxels', location, 'component joins another only by edges or corners (26-connected, not 6-connected)')

    # Bridges in the face-adjacency graph are exactly the single-face attachments.
    entered, lowest, parent, subtree, clock = {}, {}, {}, {}, 0
    for start in sorted(voxels):
        if start in entered:
            continue
        entered[start] = lowest[start] = clock
        subtree[start] = int(start in anchors)
        clock += 1
        parent[start] = None
        stack = [(start, iter(adjacency[start]))]
        while stack:
            v, iterator = stack[-1]
            n = next(iterator, None)
            if n is None:
                stack.pop()
                p = parent[v]
                if p is not None:
                    lowest[p] = min(lowest[p], lowest[v])
                    subtree[p] += subtree[v]
                    if lowest[v] > entered[p] and subtree[v] in (0, anchor_counts[labels[v]]):
                        findings.add('hanging-face', 'voxels', (np.array(v) + np.array(p) + 1) * size / 2,
                                     'hanging by one face: removing this face separates the structure')
                continue
            if n == parent[v]:
                continue
            if n in entered:
                lowest[v] = min(lowest[v], entered[n])
            else:
                parent[n] = v
                entered[n] = lowest[n] = clock
                subtree[n] = int(n in anchors)
                clock += 1
                stack.append((n, iter(adjacency[n])))
    return dict(voxels=len(voxels), components=len(components))


def inspect(path, ground=None):
    path = Path(path)
    findings = Findings()
    if path.name.endswith('.vox.json'):
        stats = inspect_voxels(json.loads(path.read_text()), findings)
    else:
        level = 0
        if path.name.endswith('.mesh.json'):
            doc = json.loads(path.read_text())
            if doc.get('up', 'y') != 'y':
                raise ValueError('mesh JSON must use Y up')
            level = float(doc.get('ground', 0))
            meshes = [mesh(m.get('name', str(i)), m['positions'], m['indices'], m.get('group'), m.get('material', ''),
                           m.get('closed', False), m.get('building'), m.get('walls', ())) for i, m in enumerate(doc['meshes'])]
        elif path.suffix.lower() == '.obj':
            meshes = obj(path)
        elif path.suffix.lower() in ('.gltf', '.glb'):
            meshes = gltf(path)
        else:
            raise ValueError(f'unsupported input: {path}')
        if not meshes or not math.isfinite(level if ground is None else ground):
            raise ValueError('no meshes or non-finite ground')
        stats = inspect_meshes(meshes, level if ground is None else ground, findings)
    counts, rows = findings.finish()
    return dict(file=str(path.resolve()), counts=counts, findings=rows, **stats)


def report(results, out):
    block, review = [], []
    for r in results:
        for f in r['findings']:
            sample = f['samples'][0]
            text = f'{f["check"]}: {f["count"]} in {f["group"]}; {sample["detail"]} at {sample["location"]}'
            if f['check'] == 'z-fight':
                text += f'; total overlap area={f["measure"]:.8g}'
            if f['by_location']:
                text += '; locations: ' + ', '.join(f'{p}={n}' for p, n in sorted(f['by_location'].items()))
            (block if f['severity'] == 'block' else review).append(text)
    print(f'meshcheck: {len(results)} inputs, {len(block)} blocking, {len(review)} for the reviewer')
    for heading, lines in [('blocking (clear faults, fix these):', block), ('for the reviewer (leads, judge against the geometry):', review)]:
        print('\n' + heading)
        for line in lines:
            print('  ' + line)
        if not lines:
            print('  none')
    print(f'\nfindings: {out}/findings.json')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--ground', type=float)
    ap.add_argument('files', nargs='+')
    args = ap.parse_args(argv)
    start = time.monotonic()
    try:
        numpy()
        out = Path(args.out or tempfile.mkdtemp(prefix='glevels-meshcheck-'))
        out.mkdir(parents=True, exist_ok=True)
        results = [inspect(p, args.ground) for p in args.files]
        payload = dict(inputs=results, seconds=round(time.monotonic() - start, 3), out=str(out.resolve()))
        (out / 'findings.json').write_text(json.dumps(payload, indent=2) + '\n')
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            report(results, out)
        return int(any(f['severity'] == 'block' for r in results for f in r['findings']))
    except (ImportError, OSError, ValueError, KeyError, IndexError, TypeError, struct.error, MemoryError, OverflowError) as error:
        print(f'impossible: {error}')
        return 2


if __name__ == '__main__':
    sys.exit(main())
