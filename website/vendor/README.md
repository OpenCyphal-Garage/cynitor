# Vendored libraries

Served from here rather than a CDN so the dashboard works on machines with no
internet access (lab benches, field laptops). Copied unmodified from the npm
packages, whose tarballs were checked against the registry's sha512 integrity.

| File | Package | License |
|------|---------|---------|
| `d3.min.js` | [d3](https://www.npmjs.com/package/d3) 7.9.0, `dist/d3.min.js` | ISC, `LICENSE-d3.txt` |
| `tabulator.min.js` | [tabulator-tables](https://www.npmjs.com/package/tabulator-tables) 6.4.0, `dist/js/tabulator.min.js` | MIT, `LICENSE-tabulator.txt` |
| `tabulator_midnight.min.css` | tabulator-tables 6.4.0, `dist/css/tabulator_midnight.min.css` | MIT, `LICENSE-tabulator.txt` |
| `fonts/ibm-plex-sans-latin-{400,500,600,700}-normal.woff2` | [@fontsource/ibm-plex-sans](https://www.npmjs.com/package/@fontsource/ibm-plex-sans) 5.3.0, `files/` | SIL OFL 1.1, `fonts/LICENSE-ibm-plex-sans.txt` |
| `fonts/ibm-plex-mono-latin-{400,500,600}-normal.woff2` | [@fontsource/ibm-plex-mono](https://www.npmjs.com/package/@fontsource/ibm-plex-mono) 5.3.0, `files/` | SIL OFL 1.1, `fonts/LICENSE-ibm-plex-mono.txt` |

The fonts are the Latin subset, in the weights `styles.css` uses; other scripts
fall back to the system fonts named after them.

To upgrade, replace the files with the same paths from the new package
version and update this table.
