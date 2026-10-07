// Resolves the bare specifier `playwright` to the stub, so the REAL script runs
// with every other line intact. Registered with node --import.
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';
register(pathToFileURL(new URL('./x-post-browser-hooks.mjs', import.meta.url).pathname));
