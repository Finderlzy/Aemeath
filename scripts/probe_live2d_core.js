/**
 * Load a Live2D model in the pinned client's Cubism Core and drive a parameter.
 *
 * This is the "load in the fixed client Core and drive it" half of V21-T01. It
 * uses the *client's own* `live2dcubismcore.js`, not the editor's Java Core, so
 * a pass here means the model actually works in Aemeath rather than merely in
 * Cubism.
 *
 * The check is deliberately not "the model loaded". Loading proves nothing about
 * deformation, so the script reads the vertex positions of a drawable, writes a
 * parameter value, updates the model, and compares the vertices: the evidence is
 * that the geometry moved.
 *
 * Usage:
 *     node scripts/probe_live2d_core.js <path-to.model3.json>
 *
 * Exit code 0 when the model loads and at least one parameter moves geometry.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const CORE = path.join(
  ROOT,
  'vendor',
  'Open-LLM-VTuber',
  'frontend',
  'libs',
  'live2dcubismcore.min.js'
);

/** The client's Core, exposed by loading its bundle. */
function loadCore() {
  // The Core probes for a DOM while initialising. Only a stub is needed: it
  // never draws, and the loading path exercised here is CPU-side.
  const noop = () => {};
  const sandbox = {
    document: {
      createElement: () => ({ getContext: () => ({}), style: {}, addEventListener: noop }),
      addEventListener: noop,
      documentElement: { style: {} },
    },
    navigator: { userAgent: 'node' },
    console,
    Math,
    Date,
    Float32Array,
    Uint8Array,
    Uint16Array,
    Int32Array,
    ArrayBuffer,
    DataView,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Error,
    TypeError,
    Promise,
    setTimeout,
    clearTimeout,
    performance,
    // The Core is a WebAssembly build and decodes its module from a base64
    // string, so it needs `atob` the same way a browser provides it.
    atob: (data) => Buffer.from(data, 'base64').toString('binary'),
    btoa: (data) => Buffer.from(data, 'binary').toString('base64'),
    WebAssembly,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;

  // The bundle declares `var Live2DCubismCore`, which stays inside its own
  // scope under a plain eval. Running it in an explicit vm context lets the
  // declaration land on the sandbox, which is where it is then read from.
  const vm = require('vm');
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CORE, 'utf8'), sandbox, { filename: CORE });

  const core = sandbox.Live2DCubismCore;
  if (!core) {
    throw new Error('Live2DCubismCore did not initialise from the client bundle');
  }
  return core;
}

/**
 * Wait for the Core's WebAssembly module to finish preparing.
 *
 * The Core instantiates its wasm asynchronously. `Moc.fromArrayBuffer` called
 * before that settles throws an opaque "reading 'apply'" from the emscripten
 * bindings, so the readiness promise is awaited first. `Utils` exposes no
 * documented readiness hook, so the module is polled until it can report a
 * version -- the first call that actually needs the wasm instance.
 */
async function waitForCore(core, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const version = core.Version.csmGetVersion();
      if (version) return version;
    } catch (err) {
      // Not ready yet; fall through to the wait.
    }
    if (Date.now() > deadline) {
      throw new Error('Cubism Core wasm did not become ready in time');
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

/** Read a parameter list index by id. */
function parameterIndex(model, id) {
  const ids = model.parameters.ids;
  for (let i = 0; i < ids.length; i += 1) {
    if (ids[i] === id) return i;
  }
  return -1;
}

/** Snapshot every drawable's vertex positions, so movement can be compared. */
function vertexSnapshot(model) {
  const drawables = model.drawables;
  const count = drawables.vertexPositions.length;
  const snapshot = [];
  for (let i = 0; i < count; i += 1) {
    snapshot.push(Float32Array.from(drawables.vertexPositions[i]));
  }
  return snapshot;
}

/** How far any vertex moved between two snapshots. */
function maxDisplacement(before, after) {
  let max = 0;
  for (let i = 0; i < before.length; i += 1) {
    const a = before[i];
    const b = after[i];
    for (let j = 0; j < a.length; j += 1) {
      const d = Math.abs(a[j] - b[j]);
      if (d > max) max = d;
    }
  }
  return max;
}

async function main() {
  const model3Path = process.argv[2];
  if (!model3Path) {
    console.log('usage: node scripts/probe_live2d_core.js <path-to.model3.json>');
    return 2;
  }
  if (!fs.existsSync(model3Path)) {
    console.log(`FAIL  model3.json not found: ${model3Path}`);
    return 1;
  }
  if (!fs.existsSync(CORE)) {
    console.log(`FAIL  client Core not found: ${CORE}`);
    return 1;
  }

  const core = loadCore();
  const coreVersion = await waitForCore(core);
  console.log(`Core csmGetVersion = ${coreVersion}`);
  console.log(`Core MocVersion_50 = ${core.MocVersion_50} (client ceiling is 5)`);

  const model3 = JSON.parse(fs.readFileSync(model3Path, 'utf8'));
  const baseDir = path.dirname(model3Path);
  const refs = model3.FileReferences || {};

  const mocPath = path.join(baseDir, refs.Moc || '');
  if (!refs.Moc || !fs.existsSync(mocPath)) {
    console.log(`FAIL  moc3 referenced by model3.json is missing: ${refs.Moc}`);
    return 1;
  }

  // The version in the file header is what the Core refuses when it is too new.
  const head = fs.readFileSync(mocPath).subarray(0, 8);
  const magic = head.subarray(0, 4).toString('ascii');
  const mocVersion = head.readInt32LE(4);
  console.log(`moc3: ${path.basename(mocPath)} magic=${magic} version=${mocVersion}`);
  if (magic !== 'MOC3') {
    console.log('FAIL  file is not a moc3');
    return 1;
  }
  if (mocVersion > 5) {
    console.log(
      `FAIL  moc3 version ${mocVersion} exceeds the client Core ceiling of 5; ` +
        're-export with an older "Export version" in Cubism'
    );
    return 1;
  }

  // Reviving the model is the real load test: this is where a bad export fails.
  // The Core wants a Uint8Array over the exact file bytes, so the Node Buffer is
  // copied rather than passed through (a Buffer's backing store is pooled and
  // larger than the file).
  const mocBytes = new Uint8Array(fs.readFileSync(mocPath));
  let moc;
  try {
    moc = core.Moc.fromArrayBuffer(mocBytes.buffer);
  } catch (err) {
    console.log(`FAIL  Core rejected the moc3: ${err && err.message ? err.message : err}`);
    return 1;
  }
  if (!moc) {
    console.log('FAIL  Core returned no moc');
    return 1;
  }
  console.log('PASS  client Core revived the moc3');

  const model = core.Model.fromMoc(moc);
  const paramIds = model.parameters.ids;
  console.log(`PASS  model built: ${paramIds.length} parameters`);

  // Which parameter to drive: prefer mouth, then eye, else the first one.
  const preferred = ['ParamMouthOpenY', 'ParamEyeLOpen', 'ParamEyeROpen', 'ParamAngleX'];
  let target = preferred.find((id) => parameterIndex(model, id) >= 0);
  if (!target) {
    target = paramIds[0];
    console.log(`note  none of ${preferred.join(', ')} exist; using ${target}`);
  }
  const index = parameterIndex(model, target);

  const values = model.parameters.values;
  const original = values[index];
  const min = model.parameters.minimumValues[index];
  const max = model.parameters.maximumValues[index];
  console.log(`target parameter: ${target} = ${original} (range ${min}..${max})`);

  const before = vertexSnapshot(model);
  // Drive it to the far end of its range so the change cannot be rounding.
  const driven = original === max ? min : max;
  values[index] = driven;
  model.update();
  const after = vertexSnapshot(model);

  const moved = maxDisplacement(before, after);
  console.log(`PASS  parameter set to ${driven}; max vertex displacement = ${moved.toFixed(6)}`);

  if (moved <= 0) {
    console.log(
      'FAIL  no geometry moved: the parameter is not bound to anything, so the ' +
        'model is loaded but not driven'
    );
    return 1;
  }

  console.log('OK    model loads in the client Core and the parameter drives geometry');
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.log(`FAIL  ${err && err.stack ? err.stack : err}`);
    process.exit(1);
  });
