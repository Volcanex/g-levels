import base64
import json
from pathlib import Path
import struct
import subprocess
import sys

import numpy as np
import pytest

from glevels import meshcheck as mc


@pytest.fixture(autouse=True)
def load_numpy():
    mc.numpy()


def cube(offset=(0, 0, 0), name='cube', missing=False):
    positions = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                          [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) + offset
    indices = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
               [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2],
               [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5]]
    return dict(name=name, group=name, material=name, closed=True,
                positions=positions.ravel().tolist(), indices=np.array(indices[:-2] if missing else indices).ravel().tolist())


def quad(points, name='quad', material=''):
    return dict(name=name, group=name, material=material, closed=False,
                positions=np.array(points).ravel().tolist(), indices=[0, 1, 2, 0, 2, 3])


def check(tmp_path, meshes, ground=0):
    path = tmp_path / 'test.mesh.json'
    path.write_text(json.dumps(dict(up='y', ground=ground, meshes=meshes)))
    return mc.inspect(path)


def rows(result, kind):
    return [f for f in result['findings'] if f['check'] == kind]


def test_clean_closed_cube_on_ground(tmp_path):
    result = check(tmp_path, [cube()])
    assert result['findings'] == []


def test_t_junction_crack_blocks_and_names_missing_join(tmp_path):
    left = quad([[0, 0, 0], [1, 0, 0], [1, 2, 0], [0, 2, 0]], 'left')
    right = quad([[1, 0, 0], [2, 0, 0], [2, 1, 0], [1, 1, 0]], 'right')
    result = check(tmp_path, [left, right])
    assert result['counts']['t-junction'] == 1
    assert rows(result, 't-junction-crack')[0]['severity'] == 'block'
    assert 'missing joinUp' in rows(result, 't-junction-crack')[0]['samples'][0]['detail']
    assert rows(result, 't-junction-crack')[0]['samples'][0]['location'] == [1, 1, 0]
    assert not rows(result, 'z-fight')


def test_repeated_t_junctions_block(tmp_path):
    meshes = []
    for k in range(mc.BLOCKING_COUNT + 1):
        x = k * 4
        meshes.extend([quad([[x, 0, 0], [x+1, 0, 0], [x+1, 2, 0], [x, 2, 0]], f'left{k}'),
                       quad([[x+1, 0, 0], [x+2, 0, 0], [x+2, 1, 0], [x+1, 1, 0]], f'right{k}')])
    assert all(r['severity'] == 'block' for r in rows(check(tmp_path, meshes), 't-junction-crack'))


def test_fan_of_joined_ring_can_reintroduce_a_t_junction(tmp_path):
    joined = dict(name='fan', positions=[0, 0, 0, 2, 0, 0, 2, 2, 0, 0, 2, 0, 0, 1, 0],
                  indices=[0, 1, 2, 0, 2, 3, 0, 3, 4])
    neighbor = quad([[-1, 0, 0], [0, 0, 0], [0, 1, 0], [-1, 1, 0]], 'neighbor')
    result = check(tmp_path, [joined, neighbor])
    assert result['counts']['degenerate'] == 1
    assert result['counts']['t-junction'] == 1
    fan = rows(result, 't-junction-fan')
    assert fan[0]['severity'] == 'block'
    assert 'fan from a collinear corner: triangulate from a non-collinear corner or ear-clip' in fan[0]['samples'][0]['detail']
    assert fan[0]['samples'][0]['degenerate_triangles'] == [2]
    assert not rows(result, 't-junction-crack')


@pytest.mark.parametrize('reverse', [False, True])
def test_coplanar_overlap_including_opposite_winding(tmp_path, reverse):
    a = quad([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]], 'a')
    b = quad([[1, 0, 0], [3, 0, 0], [3, 2, 0], [1, 2, 0]], 'b')
    if reverse:
        b['indices'] = np.array(b['indices']).reshape(-1, 3)[:, ::-1].ravel().tolist()
    result = check(tmp_path, [a, b])
    assert sum(r['measure'] for r in rows(result, 'z-fight')) == pytest.approx(2)
    assert all(s['location'][2] == 0 for r in rows(result, 'z-fight') for s in r['samples'])


def test_materials_distinguish_surfaces_and_separated_planes_pass(tmp_path):
    points = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    a, b = quad(points, 'same', 'a'), quad(points, 'same', 'b')
    assert rows(check(tmp_path, [a, b]), 'z-fight')
    b['positions'] = (np.array(points) + [0, 0, .01]).ravel().tolist()
    assert not rows(check(tmp_path, [a, b]), 'z-fight')


def test_repeated_coplanar_overlaps_block(tmp_path):
    points = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    small = check(tmp_path, [quad(points, 'a'), quad(points, 'b')])
    assert all(r['severity'] == 'review' for r in rows(small, 'z-fight'))
    repeated = check(tmp_path, [quad(points, name) for name in ['a', 'b', 'c']])
    assert repeated['counts']['z-fight'] > mc.BLOCKING_COUNT
    assert all(r['severity'] == 'block' for r in rows(repeated, 'z-fight'))


@pytest.mark.parametrize('reverse', [False, True])
def test_edge_on_plaster_inside_facade(tmp_path, reverse):
    facade = quad([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]], 'walls')
    plaster = quad([[.3, .3, 0], [.3, 1.7, 0], [.3, 1.7, -1], [.3, .3, -1]], 'plain')
    if reverse:
        facade['indices'] = np.array(facade['indices']).reshape(-1, 3)[:, ::-1].ravel().tolist()
        plaster['positions'] = (np.array(plaster['positions']).reshape(-1, 3) * [1, 1, -1]).ravel().tolist()
    result = check(tmp_path, [facade, plaster])
    assert rows(result, 'bleed-through')[0]['severity'] == 'block'
    assert rows(result, 'bleed-through')[0]['samples'][0]['centroid_depth'] < 0
    assert sum(r['measure'] for r in rows(result, 'bleed-through')) == pytest.approx(1.4, abs=1e-5)
    assert not rows(result, 'z-fight')


def test_cube_missing_one_face_declared_closed(tmp_path):
    result = check(tmp_path, [cube(missing=True)])
    assert result['counts']['open-edge'] == 4
    assert rows(result, 'open-edge')[0]['severity'] == 'block'


def test_floating_cube_and_ground_override(tmp_path):
    result = check(tmp_path, [cube((0, 2, 0))])
    assert result['counts']['floating'] == 1
    assert rows(result, 'floating')[0]['severity'] == 'block'
    assert not rows(check(tmp_path, [cube((0, 2, 0))], ground=2), 'floating')


def test_vertex_to_face_contact_reaches_ground(tmp_path):
    result = check(tmp_path, [cube(), cube((.2, 1, .2), 'supported')])
    assert not rows(result, 'floating')


@pytest.mark.parametrize('offset, word', [((1, 1, 1), 'vertex'), ((1, 1, 0), 'edge')])
def test_meshes_with_weak_contacts(tmp_path, offset, word):
    result = check(tmp_path, [cube(), cube(offset, 'attached')])
    assert not rows(result, 'floating')
    assert any(word in s['detail'] for r in rows(result, 'attachment') for s in r['samples'])


def test_degenerate_and_non_manifold_are_leads(tmp_path):
    part = dict(name='part', positions=[0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, -1, 0],
                indices=[0, 1, 2, 0, 1, 3, 0, 1, 4, 0, 0, 1])
    result = check(tmp_path, [part])
    assert result['counts']['degenerate'] == 1
    assert result['counts']['non-manifold'] == 1
    assert all(r['severity'] == 'review' for r in result['findings'])


def test_voxel_l_floating_edge_only_and_single_face(tmp_path):
    path = tmp_path / 'test.vox.json'
    path.write_text(json.dumps(dict(size=.5, voxels=[[0, 0, 0], [0, 1, 0], [1, 1, 0], [2, 2, 0], [8, 4, 8]], anchored='min-y')))
    result = mc.inspect(path)
    assert result['counts']['floating'] == 2
    assert result['counts']['attachment'] == 1
    assert result['counts']['hanging-face'] == 2
    assert rows(result, 'floating')[0]['severity'] == 'block'


def test_voxel_anchors_and_solid_block(tmp_path):
    path = tmp_path / 'test.vox.json'
    voxels = [[i, j, k] for i in range(2) for j in range(2) for k in range(2)]
    path.write_text(json.dumps(dict(size=1, voxels=voxels, anchored=[[0, 1, 0]])))
    assert mc.inspect(path)['findings'] == []


def test_face_bridge_between_two_anchors_is_supported(tmp_path):
    path = tmp_path / 'bridge.vox.json'
    path.write_text(json.dumps(dict(size=1, voxels=[[0, 0, 0], [1, 0, 0]], anchored='min-y')))
    assert mc.inspect(path)['findings'] == []


def test_large_translated_overlap_retains_area(tmp_path):
    origin = np.array([1e8, 0, 1e8])
    a = np.array([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]]) + origin
    b = a + [1, 0, 0]
    result = check(tmp_path, [quad(a, 'a'), quad(b, 'b')])
    assert sum(r['measure'] for r in rows(result, 'z-fight')) == pytest.approx(2)


def test_obj_polygons_negative_indices_and_materials(tmp_path):
    path = tmp_path / 'test.obj'
    path.write_text('v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\ng facade\nusemtl brick\nf -4/1 -3/2 -2/3 -1/4\n')
    meshes = mc.obj(path)
    assert meshes[0]['group'] == 'facade'
    assert meshes[0]['material'] == 'brick'
    assert meshes[0]['indices'].tolist() == [[0, 1, 2], [0, 2, 3]]
    assert not rows(mc.inspect(path), 'floating')


def gltf_document():
    positions = np.array([[0, 0, 0, 123], [1, 0, 0, 123], [0, 1, 0, 123]], dtype='<f4').tobytes()
    indices = np.array([0, 1, 2], dtype='<u2').tobytes()
    data = positions + indices
    doc = dict(asset=dict(version='2.0'), buffers=[dict(byteLength=len(data))],
               bufferViews=[dict(buffer=0, byteOffset=0, byteLength=len(positions), byteStride=16),
                            dict(buffer=0, byteOffset=len(positions), byteLength=len(indices))],
               accessors=[dict(bufferView=0, componentType=5126, count=3, type='VEC3'),
                          dict(bufferView=1, componentType=5123, count=3, type='SCALAR')],
               meshes=[dict(primitives=[dict(attributes=dict(POSITION=0), indices=1)])],
               nodes=[dict(translation=[5, 2, 0], children=[1]), dict(mesh=0, scale=[2, 3, 1])],
               scenes=[dict(nodes=[0])], scene=0)
    return doc, data


@pytest.mark.parametrize('kind', ['embedded', 'external', 'glb'])
def test_gltf_stride_indices_and_nested_transforms(tmp_path, kind):
    doc, data = gltf_document()
    if kind == 'glb':
        document = json.dumps(doc).encode()
        document += b' ' * (-len(document) % 4)
        data += b'\0' * (-len(data) % 4)
        body = struct.pack('<II', len(document), 0x4e4f534a) + document + struct.pack('<II', len(data), 0x004e4942) + data
        path = tmp_path / 'test.glb'
        path.write_bytes(struct.pack('<4sII', b'glTF', 2, len(body) + 12) + body)
    else:
        if kind == 'embedded':
            doc['buffers'][0]['uri'] = 'data:application/octet-stream;base64,' + base64.b64encode(data).decode()
        else:
            (tmp_path / 'test.bin').write_bytes(data)
            doc['buffers'][0]['uri'] = 'test.bin'
        path = tmp_path / 'test.gltf'
        path.write_text(json.dumps(doc))
    result = mc.gltf(path)
    assert result[0]['positions'].tolist() == [[5, 2, 0], [7, 2, 0], [5, 5, 0]]
    assert mc.inspect(path)['counts']['floating'] == 1


def test_gltf_column_major_matrix(tmp_path):
    doc, data = gltf_document()
    doc['buffers'][0]['uri'] = 'data:application/octet-stream;base64,' + base64.b64encode(data).decode()
    doc['nodes'][1] = dict(mesh=0, matrix=[0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 4, 0, 0, 1])
    path = tmp_path / 'matrix.gltf'
    path.write_text(json.dumps(doc))
    assert mc.gltf(path)[0]['positions'].tolist() == [[9, 2, 0], [9, 3, 0], [8, 2, 0]]


def test_gltf_quaternion_and_scale_order(tmp_path):
    doc, data = gltf_document()
    doc['buffers'][0]['uri'] = 'data:application/octet-stream;base64,' + base64.b64encode(data).decode()
    doc['nodes'][1]['rotation'] = [0, 0, 2**-.5, 2**-.5]
    path = tmp_path / 'rotated.gltf'
    path.write_text(json.dumps(doc))
    np.testing.assert_allclose(mc.gltf(path)[0]['positions'], [[5, 2, 0], [5, 4, 0], [2, 2, 0]])


def test_sparse_unindexed_gltf(tmp_path):
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype='<f4')
    data = bytes([0, 1, 2, 0]) + positions.tobytes()
    doc = dict(asset=dict(version='2.0'), buffers=[dict(byteLength=len(data), uri='data:application/octet-stream;base64,' + base64.b64encode(data).decode())],
               bufferViews=[dict(buffer=0, byteLength=3), dict(buffer=0, byteOffset=4, byteLength=36)],
               accessors=[dict(componentType=5126, count=3, type='VEC3', sparse=dict(count=3, indices=dict(bufferView=0, componentType=5121), values=dict(bufferView=1)))],
               meshes=[dict(primitives=[dict(attributes=dict(POSITION=0))])], nodes=[dict(mesh=0)], scenes=[dict(nodes=[0])])
    path = tmp_path / 'sparse.gltf'
    path.write_text(json.dumps(doc))
    result = mc.gltf(path)[0]
    np.testing.assert_array_equal(result['positions'], positions)
    assert result['indices'].tolist() == [[0, 1, 2]]


def test_nearby_vertices_weld_across_hash_boundaries():
    points = np.array([[.99, 0, 0], [1.01, 0, 0]])
    _, ids = mc.weld(points, .1)
    assert ids[0] == ids[1]


def test_cli_output_exit_codes_and_json(tmp_path, capsys):
    path = tmp_path / 'cube.mesh.json'
    path.write_text(json.dumps(dict(meshes=[cube()])))
    out = tmp_path / 'report'
    assert mc.main(['--out', str(out), str(path)]) == 0
    assert 'blocking (clear faults, fix these):\n  none' in capsys.readouterr().out
    assert json.loads((out / 'findings.json').read_text())['inputs'][0]['findings'] == []
    path.write_text(json.dumps(dict(meshes=[cube(missing=True)])))
    assert mc.main(['--json', '--out', str(out), str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload['inputs'][0]['counts']['open-edge'] == 4
    assert mc.main(['--out', str(out), str(tmp_path / 'absent.obj')]) == 2
    assert capsys.readouterr().out.startswith('impossible:')


@pytest.mark.parametrize('document', [dict(meshes=[]), dict(meshes=[dict(name='bad', positions=[0, 0, 0], indices=[0, 1, 2])]),
                                     dict(up='z', meshes=[cube()]), dict(meshes=[dict(name='bad', positions=[0, 0, 0], indices=[.1, 0, 0])])])
def test_invalid_input_is_impossible(tmp_path, document, capsys):
    path = tmp_path / 'invalid.mesh.json'
    path.write_text(json.dumps(document))
    assert mc.main(['--out', str(tmp_path / 'out'), str(path)]) == 2
    assert capsys.readouterr().out.startswith('impossible:')


def test_missing_numpy_is_impossible(tmp_path, monkeypatch, capsys):
    def unavailable():
        raise ImportError('numpy is unavailable')
    monkeypatch.setattr(mc, 'numpy', unavailable)
    assert mc.main([str(tmp_path / 'unused.mesh.json')]) == 2
    assert 'impossible: numpy is unavailable' in capsys.readouterr().out


def test_direct_script_invocation(tmp_path):
    path = tmp_path / 'cube.mesh.json'
    path.write_text(json.dumps(dict(meshes=[cube()])))
    result = subprocess.run([sys.executable, str(Path(mc.__file__)), '--out', str(tmp_path / 'out'), str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('reverse', [False, True])
def test_front_side_contact_is_not_a_finding(tmp_path, reverse):
    facade = quad([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]], 'walls')
    side = 1 if not reverse else -1
    frame = quad([[.3, .3, 0], [.3, 1.7, 0], [.3, 1.7, side], [.3, .3, side]], 'trim')
    if reverse:
        facade['indices'] = np.array(facade['indices']).reshape(-1, 3)[:, ::-1].ravel().tolist()
    assert not rows(check(tmp_path, [facade, frame]), 'bleed-through')


def test_hidden_target_surface_does_not_report_bleed(tmp_path):
    facade = quad([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]], 'hidden')
    inner = quad([[.3, .3, 0], [.3, 1.7, 0], [.3, 1.7, -1], [.3, .3, -1]], 'inner')
    cover = quad([[0, 0, .2], [2, 0, .2], [2, 2, .2], [0, 2, .2]], 'cover')
    assert not rows(check(tmp_path, [facade, inner, cover]), 'bleed-through')


def test_bleed_with_same_material_and_group(tmp_path):
    facade = quad([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]], 'same')
    inner = quad([[.3, .3, 0], [.3, 1.7, 0], [.3, 1.7, -1], [.3, .3, -1]], 'same')
    assert rows(check(tmp_path, [facade, inner]), 'bleed-through')


def test_fan_chain_and_unrelated_degenerate_piece(tmp_path):
    fan = dict(name='fan', positions=[0,0,0, 2,0,0, 2,3,0, 0,3,0, 0,2,0, 0,1,0],
               indices=[0,1,2, 0,2,3, 0,3,4, 0,4,5])
    neighbors = [quad([[-1,y,0], [0,y,0], [0,y+1,0], [-1,y+1,0]], f'n{y}') for y in range(2)]
    result = check(tmp_path, [fan, *neighbors])
    assert result['counts']['t-junction-fan'] == 2
    assert not rows(result, 't-junction-crack')
    fan['indices'] = fan['indices'][:6]
    unrelated = dict(name='other', positions=[0,0,0, 0,3,0, 0,1,0], indices=[0,1,2])
    result = check(tmp_path, [fan, *neighbors, unrelated])
    assert result['counts']['t-junction-crack'] == 2
    assert not rows(result, 't-junction-fan')


def test_counts_keep_every_building_and_wall_beyond_sample_limit(tmp_path, capsys):
    meshes = []
    for k in range(mc.SAMPLES + 2):
        x = 4*k
        a = quad([[x,0,0], [x+1,0,0], [x+1,2,0], [x,2,0]], f'a{k}')
        b = quad([[x+1,0,0], [x+2,0,0], [x+2,1,0], [x+1,1,0]], f'b{k}')
        a['building'] = b['building'] = f'building{k}'
        a['walls'] = b['walls'] = [dict(id=3, a=[x,0], b=[x+2,0], normal=[0,0,1], thickness=.25)]
        a['group'] = b['group'] = 'walls'
        meshes.extend([a,b])
    result = check(tmp_path, meshes)
    finding = rows(result, 't-junction-crack')[0]
    assert len(finding['samples']) == mc.SAMPLES
    assert finding['by_building'] == {f'building{k}': 1 for k in range(mc.SAMPLES+2)}
    assert finding['by_location'] == {f'building{k}/wall-3': 1 for k in range(mc.SAMPLES+2)}
    mc.report([result], tmp_path)
    assert f'building{mc.SAMPLES+1}/wall-3=1' in capsys.readouterr().out


def test_contact_on_face_boundary_and_outside_area_is_not_bleed(tmp_path):
    facade = quad([[0,0,0], [2,0,0], [2,2,0], [0,2,0]], 'facade')
    for x in [0, 3]:
        inside = quad([[x,.3,0], [x,1.7,0], [x,1.7,-1], [x,.3,-1]], 'inside')
        assert not rows(check(tmp_path, [facade, inside]), 'bleed-through')


def test_adapter_orients_all_batches_and_exports_wall_locations(tmp_path):
    repo = tmp_path / 'engine'
    modules = {
        'v2/tests/range/world.mjs': "export const CELL='cell', GROUND=0; export function rangeDoc(){return {chunk:{roofs:[]}}} export function rangeHeights(){return {}}",
        'v2/sim/generator/index.mjs': "export function generateCell(){return {classes:[{components:{building:{id:'building',record:{w:[[0,0],[2,0],[2,2],[0,2]]},walls:[{edge:0,len:2,t:.25}]}}}]}}",
        'v2/sim/generator/geometry.mjs': 'export const signedArea=()=>4, ringOf=w=>w;',
        'v2/sim/generator/rules.mjs': 'export const WALL_T=.25;',
        'v2/client/lib/building-model.mjs': """
            export function drawBuildingInteriors(){}
            export function drawBuildingModel(out){
                for(const [i,group] of ['walls','plain','trim','lib'].entries()){
                    const normal=[0,0,i%2 ? -1 : 1];
                    out[group].tri(...[[0,0,0],[1,0,0],[0,1,0]].map(position=>({position,normal})));
                }
            }
        """,
    }
    for filename, source in modules.items():
        path = repo / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    output = tmp_path / 'adapter.mesh.json'
    result = subprocess.run(['node', str(Path(mc.__file__).parent / 'adapters/g_engine_mesh.mjs'),
                             '--repo', str(repo), '--out', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    meshes = json.loads(output.read_text())['meshes']
    assert len(meshes) == 4
    for i, mesh in enumerate(meshes):
        a,b,c = np.array(mesh['positions']).reshape(-1,3)
        assert np.cross(b-a,c-a)[2] == (-1 if i%2 else 1)
        assert mesh['building'] == 'building'
        assert mesh['walls'] == [dict(id=0, a=[0,0], b=[2,0], thickness=.25, normal=[0,0,-1])]


def test_building_count_deduplicates_two_walls_at_a_corner():
    findings = mc.Findings()
    findings.add('bleed-through', 'plain / walls', [0,0,0], 'corner', severity='block',
                 where=['building/wall-0', 'building/wall-1'])
    _, result = findings.finish()
    assert result[0]['by_building'] == {'building': 1}
    assert result[0]['by_location'] == {'building/wall-0': 1, 'building/wall-1': 1}
