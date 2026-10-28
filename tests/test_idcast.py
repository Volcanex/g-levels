from glevels import idcast


def test_original_library_layer_is_caught():
    src = """
      const layer = attribute('layer', 'float');
      const c = stripSample(strip, L, layer, attribute('muv', 'vec2')).rgb;
      m.roughnessNode = perLayer.element(int(layer)).w;
    """
    assert [n for n, _ in idcast.inspect(src)] == [4]


def test_direct_cast_of_an_attribute_lane_is_caught():
    src = "m.colorNode = palette.element(uint(attribute('kind', 'vec4').w));\n"
    assert len(idcast.inspect(src)) == 1


def test_rounded_marked_and_unrelated_casts_pass():
    src = """
      const layer = attribute('layer', 'float').round();
      m.roughnessNode = perLayer.element(int(layer)).w;
      const direct = int(attribute('kind', 'float').round());
      // idcast: vertex
      const entity = uint(attribute('bodyIndex', 'float'));
      const member = uint(attribute(column, 'vec4').w); // idcast: vertex
      const id = int(idN), k = int(MATERIAL_KIND[kind]);
      // the old code cast int(attribute('layer', 'float')) here
    """
    assert idcast.inspect(src) == []


def test_files_without_attributes_pass():
    assert idcast.inspect("const k = int(x.toVar());\n") == []


def test_cast_of_a_derived_index_is_caught():
    src = "const particleIndex=attribute('particleIndex','float'),index=uint(particleIndex.max(0));\n"
    assert len(idcast.inspect(src)) == 1
