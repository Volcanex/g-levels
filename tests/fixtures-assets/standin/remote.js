// ---- a small stand-in helicopter shape ----
// easy to swap for the real model later
function buildHeli(mat) {
  const hull = new THREE.Mesh(new THREE.BoxGeometry(1.5, 1.5, 4.4), mat);
  return hull;
}
