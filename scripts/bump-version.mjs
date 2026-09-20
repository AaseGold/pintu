import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const versionPath = resolve(root, 'VERSION');
const indexPath = resolve(root, 'index.html');
const readmePath = resolve(root, 'README.md');
const requested = process.argv[2];
const pattern = /^v?(\d+)\.(\d+)$/;

const current = (await readFile(versionPath, 'utf8')).trim();
const currentMatch = current.match(pattern);
if (!currentMatch) throw new Error(`Invalid VERSION value: ${current}`);

let next;
if (requested) {
  const requestedMatch = requested.match(pattern);
  if (!requestedMatch) throw new Error('Target version must use 1.0 or v1.0 format');
  next = `v${Number(requestedMatch[1])}.${Number(requestedMatch[2])}`;
} else {
  next = `v${Number(currentMatch[1])}.${Number(currentMatch[2]) + 1}`;
}

if (next === current) throw new Error(`Target version matches current version: ${current}`);

const index = await readFile(indexPath, 'utf8');
const updatedIndex = index.replace(
  /(<meta name="app-version" content=")v\d+\.\d+(">)/,
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
