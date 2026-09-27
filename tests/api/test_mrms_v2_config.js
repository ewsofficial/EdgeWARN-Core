import fs from 'fs';
import path from 'path';
import { validateDocument } from '../../src/config/loader.js';

const fixture = JSON.parse(fs.readFileSync('tests/fixtures/config/mrms_v2_validation.json', 'utf8'));

test.each(fixture.cases)('$label', (entry) => {
  const document = structuredClone(fixture.documents[entry.name]);
  let parent = document;
  for (const part of entry.path.slice(0, -1)) parent = parent[part];
  parent[entry.path.at(-1)] = entry.value;
  const validate = () => validateDocument(entry.name, document, path.resolve(`config/schema/${entry.name}.schema.json`));
  if (entry.valid) expect(validate).not.toThrow();
  else expect(validate).toThrow();
});

test('complete converted fixture catalog validates', () => {
  for (const [name, document] of Object.entries(fixture.documents)) {
    expect(() => validateDocument(name, document, path.resolve(`config/schema/${name}.schema.json`))).not.toThrow();
  }
});
