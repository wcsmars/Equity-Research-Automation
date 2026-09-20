// Generate local paths immediately before packaging. Do not commit the output.
const fs = require("fs");
const path = require("path");

const projectRoot = path.resolve(
  process.env.ERC_PROJECT_ROOT || path.join(__dirname, "..")
);
const nodePath = process.env.ERC_NODE_PATH
  ? path.resolve(process.env.ERC_NODE_PATH)
  : process.execPath;

for (const required of ["backend/app.py", "frontend/package.json"]) {
  if (!fs.existsSync(path.join(projectRoot, required))) {
    throw new Error(`Project file missing: ${path.join(projectRoot, required)}`);
  }
}
fs.accessSync(nodePath, fs.constants.X_OK);

fs.writeFileSync(
  path.join(__dirname, "runtime-paths.json"),
  JSON.stringify({ projectRoot, nodePath }, null, 2) + "\n",
  { mode: 0o600 }
);
console.log("Generated local desktop/runtime-paths.json for packaging.");
