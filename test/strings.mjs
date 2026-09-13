/**
 * Loads the shipped strings sheet, as main.js does in the browser. Import it
 * before anything that shows a text.
 */
import { readFileSync } from 'node:fs';
import { parseStrings, useStrings } from '../js/i18n.js';

useStrings(parseStrings(readFileSync(new URL('../js/strings.csv', import.meta.url), 'utf8')));
