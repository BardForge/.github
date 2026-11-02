import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const paths = execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean);
const forbidden =
  /(^|\/)(?:\.?superpowers|\.codex|private|internal|node_modules|tmp|test-results|playwright-report|models)(\/|$)|(^|\/)(?:AGENTS|CLAUDE)\.md$|BardForge_(?:Studio_Concept_TZ|GIT_AGENT_RULES)|(^|\/)\.env(?:\.|$)|\.(?:pem|key|sqlite3?|db|wav|mp3|ogg|flac)$/i;
const secretPatterns = [
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
  /\bgh[pousr]_[A-Za-z0-9]{30,}\b/,
  /\bgithub_pat_[A-Za-z0-9_]{40,}\b/,
  /\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b/,
];
const problems = [];
for (const file of paths) {
  if (!process.argv.includes('--secrets-only') && forbidden.test(file) && file !== '.env.example')
    problems.push(`Private or generated path: ${file}`);
  if (process.argv.includes('--paths-only')) continue;
  if (/\.(?:png|jpg|webp|ico|woff2?)$/i.test(file)) continue;
  const text = readFileSync(file, 'utf8');
  if (secretPatterns.some((pattern) => pattern.test(text))) problems.push(`Possible credential in: ${file}`);
}
if (problems.length) {
  console.error(problems.join('\n'));
  process.exitCode = 1;
} else
  console.log(
    `PASS: ${paths.length} tracked paths checked; no prohibited files or recognized credential patterns.`,
  );
