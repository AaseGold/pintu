import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const versionPath = resolve(root, 'VERSION');
const indexPath = resolve(root, 'index.html');
const readmePath = resolve(root, 'README.md');
const requested = process.argv[2];
const pattern = /^v?(\d+)\.(\d+)(?:\.(\d+))?$/;

const current = (await readFile(versionPath, 'utf8')).trim();
const currentMatch = current.match(pattern);
if (!currentMatch) throw new Error(`Invalid VERSION value: ${current}`);

let next;
if (requested === '--release') {
  next = `v${Number(currentMatch[1])}.${Number(currentMatch[2]) + 1}`;
} else if (requested) {
  const requestedMatch = requested.match(pattern);
  if (!requestedMatch) throw new Error('Target version must use 1.0, v1.0, or a three-part version');
  next = `v${Number(requestedMatch[1])}.${Number(requestedMatch[2])}`;
  if (requestedMatch[3] !== undefined) next += `.${Number(requestedMatch[3])}`;
} else {
  const nextRevision = currentMatch[3] === undefined ? 1 : Number(currentMatch[3]) + 1;
  next = `v${Number(currentMatch[1])}.${Number(currentMatch[2])}.${nextRevision}`;
}

if (next === current) throw new Error(`Target version matches current version: ${current}`);

const index = await readFile(indexPath, 'utf8');
const updatedIndex = index.replace(
  /(<meta name="app-version" content=")v\d+\.\d+(?:\.\d+)?(">)/,
  `$1${next}$2`,
);
if (updatedIndex === index) throw new Error('app-version was not found in index.html');

const readme = await readFile(readmePath, 'utf8');
const updatedReadme = readme.replace(current, next);
if (updatedReadme === readme) throw new Error(`Current version was not found in README.md: ${current}`);

await Promise.all([
  writeFile(versionPath, `${next}\n`),
  writeFile(indexPath, updatedIndex),
  writeFile(readmePath, updatedReadme),
]);

console.log(`${current} -> ${next}`);
