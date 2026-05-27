import { execFileSync, spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";

interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  execute: (args: Record<string, string>) => Promise<string>;
}

function resolveVault(): string {
  const workspace =
    process.env.SUTANDO_WORKSPACE ??
    path.join(process.env.HOME ?? "~", ".sutando", "workspace");
  return path.join(workspace, "obsidian-vault");
}

function ensureVault(vault: string): void {
  const obsidian = path.join(vault, ".obsidian");
  const notes = path.join(vault, "Sutando", "Notes");
  const thoughts = path.join(vault, "Sutando", "Thoughts");
  const tasks = path.join(vault, "Sutando", "Tasks.md");

  fs.mkdirSync(obsidian, { recursive: true });
  fs.mkdirSync(notes, { recursive: true });
  fs.mkdirSync(thoughts, { recursive: true });

  if (!fs.existsSync(tasks)) {
    fs.writeFileSync(tasks, "# Sutando Tasks\n\n");
  }
}

function slug(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function writeNote(vault: string, title: string, body: string): string {
  ensureVault(vault);
  const filename = `${slug(title)}.md`;
  const notePath = path.join(vault, "Sutando", "Notes", filename);
  const now = new Date().toISOString().slice(0, 10);
  const content = `---\ntitle: ${title}\ndate: ${now}\n---\n\n${body}\n`;
  fs.writeFileSync(notePath, content);
  return notePath;
}

function appendTask(vault: string, body: string): void {
  ensureVault(vault);
  const tasksPath = path.join(vault, "Sutando", "Tasks.md");
  const now = new Date().toISOString();
  fs.appendFileSync(tasksPath, `\n- [ ] ${body}  <!-- ${now} -->\n`);
}

function appendThought(vault: string, body: string): void {
  ensureVault(vault);
  const now = new Date().toISOString();
  const filename = `thought-${now.slice(0, 10)}.md`;
  const thoughtPath = path.join(vault, "Sutando", "Thoughts", filename);
  const line = `\n> ${body}  <!-- ${now} -->\n`;
  fs.appendFileSync(thoughtPath, line);
}

const addToVaultTool: ToolDefinition = {
  name: "add_to_vault",
  description:
    "Add a note, task, or thought to the Obsidian vault. " +
    "kind=note writes a titled markdown note. " +
    "kind=task appends a checklist item to Tasks.md. " +
    "kind=thought appends to today's thought log.",
  parameters: {
    type: "object",
    properties: {
      kind: {
        type: "string",
        enum: ["note", "task", "thought"],
        description: "What type of content to add",
      },
      body: {
        type: "string",
        description: "The content to write",
      },
      title: {
        type: "string",
        description: "Title for kind=note (ignored for task/thought)",
      },
    },
    required: ["kind", "body"],
  },
  async execute(args) {
    const vault = resolveVault();
    const { kind, body, title } = args;

    try {
      if (kind === "note") {
        const noteTitle = title ?? `Note ${new Date().toISOString()}`;
        const notePath = writeNote(vault, noteTitle, body);
        return `Saved note to ${notePath}`;
      } else if (kind === "task") {
        appendTask(vault, body);
        return `Task added to ${path.join(vault, "Sutando", "Tasks.md")}`;
      } else if (kind === "thought") {
        appendThought(vault, body);
        return `Thought saved to Sutando/Thoughts/`;
      } else {
        return `Unknown kind: ${kind}`;
      }
    } catch (err) {
      return `Error writing to vault: ${err}`;
    }
  },
};

const runDreamTool: ToolDefinition = {
  name: "run_dream",
  description:
    "Trigger the Obsidian dream pass — an LLM sweep that discovers cross-links between vault notes. " +
    "Runs in the background (detached). Returns immediately.",
  parameters: {
    type: "object",
    properties: {},
    required: [],
  },
  async execute(_args) {
    const skillDir = path.resolve(__dirname, "..");
    const dreamScript = path.join(skillDir, "scripts", "dream.py");

    if (!fs.existsSync(dreamScript)) {
      return `dream.py not found at ${dreamScript}`;
    }

    try {
      const child = spawn(process.execPath.replace("node", "python3"), [dreamScript, "--force"], {
        detached: true,
        stdio: "ignore",
      });
      child.unref();
      return "dream.py launched in background — check obsidian-vault/Sutando/ for results";
    } catch (err) {
      return `Failed to launch dream.py: ${err}`;
    }
  },
};

export const tools: ToolDefinition[] = [addToVaultTool, runDreamTool];
