const figure = new THREE.Mesh(new THREE.SphereGeometry(1), new THREE.MeshBasicMaterial({color: '#999'}));
function particleSprite(ctx) { ctx.arc(0, 0, 10, 0, Math.PI * 2); ctx.fillRect(0, 0, 20, 20); }
function drawFace(ctx) { ctx.moveTo(0, 0); ctx.lineTo(20, 20); }
const texture = new TextureLoader().load('photo.jpg');
const model = new GLTFLoader().load('scene.glb');
