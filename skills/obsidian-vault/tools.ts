// obsidian-vault: voice-inline capture into a Sutando-owned Obsidian vault.
//
// One tool: add_to_vault(kind, body, title?). Writes to filesystem — no
// Obsidian plugin / REST API needed. Obsidian's file-watcher picks up the
// change instantly when the vault is open.
//
// Vault layout (everything under Sutando/, by kind):
//   $SUTANDO_WORKSPACE/obsidian-vault/
//     .obsidian/                          ← marker so Obsidian recognizes it
//     Sutando/
//       Notes/<slug>-<YYYY-MM-DDTHHMMSS>.md   (kind=note)
//       Tasks.md                              (kind=task, appended checkbox)
//       Thoughts/<YYYY-MM-DD>.md              (kind=thought, appended timestamped block)
//
// First call lazily initializes the vault dir + .obsidian/ + Sutando/ tree.
// To use the vault in Obsidian: File → Open vault → Open folder as vault →
// pick $SUTANDO_WORKSPACE/obsidian-vault. (One-time; Obsidian remembers.)

import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { promises as fs } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

function vaultRoot(): string {
    const ws = process.env.SUTANDO_WORKSPACE || join(homedir(), '.sutando', 'workspace');
    return join(ws, 'obsidian-vault');
}

function slugify(input: string): string {
    return (
        input
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 60) || 'untitled'
    );
}

function isoCompact(d: Date): string {
    // 2026-05-24T021530 — filename-safe, sortable
    const pad = (n: number) => String(n).padStart(2, '0');
    return (
        `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
        `T${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
    );
}

function dateOnly(d: Date): string {
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function clockTime(d: Date): string {
    return d.toLocaleTimeString('en-US', { hour12: false });
}

async function ensureVault(): Promise<string> {
    const root = vaultRoot();
    await fs.mkdir(join(root, '.obsidian'), { recursive: true });
    await fs.mkdir(join(root, 'Sutando', 'Notes'), { recursive: true });
    await fs.mkdir(join(root, 'Sutando', 'Thoughts'), { recursive: true });
    const tasks = join(root, 'Sutando', 'Tasks.md');
    try {
        await fs.access(tasks);
    } catch {
        await fs.writeFile(tasks, '# Tasks\n\n', 'utf8');
    }
    return root;
}

async function writeNote(root: string, title: string | undefined, body: string): Promise<string> {
    const now = new Date();
    const baseTitle = title?.trim() || body.split('\n', 1)[0].slice(0, 60) || 'untitled';
    const file = join(root, 'Sutando', 'Notes', `${slugify(baseTitle)}-${isoCompact(now)}.md`);
    const frontmatter = [
        '---',
        `title: ${baseTitle.replace(/"/g, '\\"')}`,
        `created: ${now.toISOString()}`,
        'source: sutando',
        '---',
        '',
    ].join('\n');
    await fs.writeFile(file, frontmatter + body.trim() + '\n', 'utf8');
    return file;
}

async function appendTask(root: string, body: string): Promise<string> {
    const file = join(root, 'Sutando', 'Tasks.md');
    const now = new Date();
    const line = `- [ ] ${body.trim().replace(/\s+/g, ' ')}  <!-- ${now.toISOString()} -->\n`;
    await fs.appendFile(file, line, 'utf8');
    return file;
}

async function appendThought(root: string, body: string): Promise<string> {
    const now = new Date();
    const file = join(root, 'Sutando', 'Thoughts', `${dateOnly(now)}.md`);
    try {
        await fs.access(file);
    } catch {
        await fs.writeFile(file, `# ${dateOnly(now)}\n\n`, 'utf8');
    }
    const block = `## ${clockTime(now)}\n\n${body.trim()}\n\n`;
    await fs.appendFile(file, block, 'utf8');
    return file;
}

const addToVaultTool: ToolDefinition = {
    name: 'add_to_vault',
    description:
        'Capture a note, task, or thought into the user\'s Obsidian vault at $SUTANDO_WORKSPACE/obsidian-vault. ' +
        'Call when the user says "save this as a note", "remember this thought", "add to my tasks", "log this", "note that X", "todo: X", or similar capture intents. ' +
        'kind="note" writes a standalone markdown file (give it a title if obvious from the body). ' +
        'kind="task" appends a checkbox to Sutando/Tasks.md. ' +
        'kind="thought" appends a timestamped block to today\'s thoughts file (Sutando/Thoughts/YYYY-MM-DD.md) — use when the user wants to record an idea or reflection rather than a deliverable. ' +
        'If the user does not specify, prefer "thought" for stream-of-consciousness, "task" if action-shaped (verb-leading or contains "I need to" / "remind me to"), "note" for everything else.',
    execution: 'inline',
    parameters: z.object({
        kind: z
            .enum(['note', 'task', 'thought'])
            .describe('What flavor of capture this is. See description for picking rules.'),
        body: z
            .string()
            .min(1)
            .describe('The content to capture. For tasks this is the action; for thoughts this is the idea; for notes this is the full markdown body.'),
        title: z
            .string()
            .optional()
            .describe('Optional title — used only for kind="note" to make the filename descriptive. Ignored for task/thought.'),
    }),
    execute: async ({ kind, body, title }: { kind: 'note' | 'task' | 'thought'; body: string; title?: string }) => {
        try {
            const root = await ensureVault();
            let file: string;
            if (kind === 'note') file = await writeNote(root, title, body);
            else if (kind === 'task') file = await appendTask(root, body);
            else file = await appendThought(root, body);
            console.log(`${ts()} [obsidian-vault] add_to_vault kind=${kind} → ${file}`);
            return {
                status: 'ok',
                kind,
                file,
                vault: root,
                note:
                    'Saved. If Obsidian does not show the vault yet, open Obsidian → File → ' +
                    'Open vault → Open folder as vault, then pick the vault path.',
            };
        } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            console.error(`${ts()} [obsidian-vault] add_to_vault failed: ${message}`);
            return { status: 'error', error: message };
        }
    },
};

const runDreamTool: ToolDefinition = {
    name: 'run_dream',
    description:
        'Kick off a Sutando Obsidian Dream pass — LLM-judged cross-linking over the vault notes. ' +
        'Call when the user says "run the dream", "dream now", "rebuild the obsidian links", "find related notes", or similar requests to re-evaluate cross-references. ' +
        'Spawns the dream.py script in the background (does NOT block); the user can check the result in Obsidian after a few seconds. ' +
        'Normally runs nightly via cron; this is the on-demand trigger. The model defaults to claude-opus-4-7 unless overridden via SUTANDO_DREAM_MODEL.',
    execution: 'inline',
    parameters: z.object({}),
    execute: async () => {
        try {
            const scriptPath = `${process.env.SUTANDO_REPO_DIR || process.cwd()}/skills/obsidian-vault/scripts/dream.py`;
            // Fire-and-forget: detach so the voice turn returns immediately.
            // `--force` bypasses the SUTANDO_OBSIDIAN_MIRROR opt-in gate
            // because this is an explicit user invocation (the user said
            // "run the dream"). The nightly cron does NOT pass --force —
            // it respects the opt-in.
            const child = (await import('node:child_process')).spawn('python3', [scriptPath, '--force'], {
                detached: true,
                stdio: 'ignore',
                env: { ...process.env },
            });
            child.unref();
            console.log(`${ts()} [obsidian-vault] run_dream → pid=${child.pid}`);
            return {
                status: 'ok',
                note: 'Dream pass started in the background. Check the vault in a few seconds for updated footer sections and inline citations.',
            };
        } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            console.error(`${ts()} [obsidian-vault] run_dream failed: ${message}`);
            return { status: 'error', error: message };
        }
    },
};

export const tools: ToolDefinition[] = [addToVaultTool, runDreamTool];
