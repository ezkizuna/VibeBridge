// SPDX-License-Identifier: GPL-3.0-or-later
// core/config.js - provider-agnostic constants: app identity, system prompt,
// feedback strings, tool categorisation. NOTHING in this file may reference a
// specific AI site (DOM, selectors, site names) - that lives in providers/*.
// eslint-disable-next-line no-unused-vars
const ZS = (() => {
  "use strict";

  // Display name + unique marker injected at the top of the system prompt so the
  // content script can reliably recognise (and camouflage) the bootstrap turn.
  const APP_NAME = "VibeBridge";
  const SYS_MARKER = "⟦ZS-SYS⟧";
  // A re-statement of the system prompt mid-session (see withSysResend in
  // core/main.js). It carries SYS_MARKER TOO - that is what drives camouflage
  // and session detection, and neither should change - plus this second marker,
  // purely so the chip can say "Reminder" instead of inheriting the bootstrap's
  // "Starting Up". Same content, different label: a re-injection is not a start.
  const RESEND_MARKER = "⟦ZS-RE⟧";

  // ── Tool → visual category (icon + colour theme for the chips) ─────────
  // VSCode MCP only. Returns one of:
  //   read | edit | screen | generate | vscode | tool
  function toolCategory(name) {
    const n = (name || "").includes("/") ? name.split("/").pop() : (name || "");
    if (n === "list_commands" || n === "list_tools") return "read";
    if (/^(read_file|list_files|grep|get_active_file|get_workspace_info|get_diagnostics|vscode_status|get_document_symbols|search_workspace_symbols|get_hover|go_to_definition|find_references)$/.test(n))
      return "read";
    if (/^(write_file|create_file|show_diff|open_file|apply_code_action|rename_symbol)$/.test(n))
      return "edit";
    if (n === "screen_capture") return "screen";
    if (/^generate_/.test(n)) return "generate";
    if (n.startsWith("vscode") || /workspace|terminal|vscode/i.test(n)) return "vscode";
    return "tool";
  }

  // Feedback strings sent back to the model so it can self-correct.
  const FEEDBACK = {
    // A command-shaped reply that could not be turned into a runnable call.
    // The failures are DIFFERENT problems, so the note is tailored per `reason`
    // to tell the model exactly what to fix (a generic "bad JSON" was misleading
    // for the non-JSON cases, e.g. a missing ###LUA### opener). Falls back to the
    // generic "malformed" text for any unrecognised reason.
    parseError: (reason, toolName) => {
      // ###LUA### is execute_luau-ONLY (the parser always maps a bare ###LUA###
      // block to execute_luau). So only suggest it when the broken command IS
      // execute_luau, or when we could not tell which command it was. For a KNOWN
      // other command (e.g. execute_blender_code) the ###LUA### hint is wrong and
      // misleading - a model that followed it would ship its code to the wrong MCP
      // - so drop it and keep the JSON-only guidance.
      const otherCmd = toolName && toolName !== "command" && toolName !== "execute_luau";
      const luaMalformed = otherCmd ? "" : " (or use the ###LUA### / ###END_LUA### block for execute_luau)";
      const luaUnclosed = otherCmd ? "" : " (or a complete ###LUA### ... ###END_LUA### block for execute_luau)";
      const objAlt = otherCmd ? "" : " (or ###...### block)";
      const notes = {
        malformed:
          "ERROR: a VibeBridge command was detected in your reply but its JSON could not be parsed. " +
          'Rewrite it as a single valid JSON object in plain text, exactly like {"command": "name", "params": {...}}' +
          luaMalformed + ". You may add a short note around it. " +
          "Please retry.",
        unclosed:
          "ERROR: your VibeBridge command was cut off before it finished - the JSON object" +
          objAlt + " never closed, so it could not run. Rewrite the WHOLE command in one " +
          'piece as valid JSON, exactly like {"command": "name", "params": {...}}' +
          luaUnclosed + ". Please retry.",
        luaOpener:
          "ERROR: you wrote the closing ###END_LUA### marker but not the opening ###LUA### marker, " +
          "so the Luau block was not detected and did not run. Put ###LUA### immediately BEFORE your " +
          "code and ###END_LUA### after it. Please retry.",
        envelope:
          "ERROR: you wrote a command's parameters as a bare JSON object, but without the required " +
          "envelope, so it was not recognised as a command. Wrap them like " +
          '{"command": "name", "params": { ...your parameters... }} - the parameter keys go INSIDE ' +
          '"params". Please retry.',
        // The model named a REAL tool but under the wrong key - it wrote the call
        // the way a function-calling API would (e.g. {"toolName": "get_studio_state",
        // "studio_id": "..."}) instead of VibeBridge's envelope. Seen live on
        // ChatGPT in a long session. Naming the wrong keys explicitly matters: a
        // generic "bad JSON" note made the model rewrite the SAME shape.
        toolKey:
          "ERROR: you used the wrong key to name the command, so it was not recognised and did not " +
          'run. The key must be exactly "command" - not "toolName", "tool", "name", "function" or ' +
          '"action" - and every argument goes INSIDE "params", like ' +
          '{"command": "name", "params": { ...your parameters... }}. Please retry.',
        // DeepSeek sometimes falls back to its own native agentic markup. The
        // note must NEVER quote the markers literally: the reply that follows
        // often echoes the wording, and a quoted marker would re-trigger the
        // detector and loop the error forever. Describe it, don't reproduce it.
        dsml:
          "ERROR: you wrote that call in your own internal tool-call markup (the DSML invoke/parameter " +
          "tags). VibeBridge cannot read that format, so the command did not run. Never use those tags " +
          "here. Write the call as a single plain-text JSON object instead, exactly like " +
          '{"command": "name", "params": { ...your parameters... }} - one command per reply. ' +
          "Please retry.",
      };
      return notes[reason] || notes.malformed;
    },
    multiTool: (names) =>
      "ERROR: You wrote multiple commands in one reply. Write ONE command at a " +
      "time and wait for its result before the next. You tried: " +
      names.join(", ") +
      ". Start over and write only the first command you need.",
    unknownTool: (name, valid) =>
      `ERROR: unknown command "${name}". It does not exist. Valid commands are: ` +
      valid.join(", ") +
      ". Use an exact name and parameter keys from the system prompt.",
    studioOffline:
      "ERROR: no VSCode instance is connected to the MCP server, so the command " +
      "could not run. VSCode is closed, has no folder open, or its MCP Bridge server " +
      "is not started. This is an environment problem on the user's machine, NOT your mistake. " +
      "Tell the user in one short sentence to open their project folder in VSCode, trust it, " +
      "then press Cmd+Shift+P > 'VS Code MCP Bridge: Start Server'. Then: if the task NEEDS VSCode, " +
      "stop until they confirm it is back; otherwise run list_mcp_servers and continue on another " +
      "connected server for anything that does not need VSCode.",
    // The page outlived the extension build it was running (reload / auto-update
    // / disable+enable). Nothing here can recover it - only a page reload can -
    // so the model must NOT be told the bridge is down and must NOT retry, or it
    // burns the whole conversation re-issuing commands that can never run. See
    // isContextInvalidated in core/main.js.
    staleExtension:
      "ERROR: the VibeBridge extension was reloaded or updated while this page was open, so this " +
      "tab is running a version of it that no longer exists and NO command can reach the user's " +
      "machine from here. The bridge and VSCode are NOT the problem - do not tell the user " +
      "to check them, and do not retry the command, because every retry will fail the same way. " +
      "Tell the user in one short sentence to RELOAD THIS PAGE (F5), then stop and wait.",
    bridgeOffline:
      "ERROR: the local VibeBridge bridge is unreachable, so no command could run. " +
      "This is an environment problem on the user's machine (the bridge is not " +
      "running, or VSCode is closed), NOT your mistake. Tell the user in " +
      "one short sentence that the bridge or VSCode is offline, then stop " +
      "sending commands until they confirm it is back.",
    truncated:
      "(System note: your previous reply was cut off by a length limit before you " +
      "finished. Continue from exactly where you stopped. Do NOT restart and do " +
      "NOT repeat what you already wrote.)",
  };

  const BT = "```";

  function compactTools(tools) {
    return (tools || [])
      .map((t) => {
        const name = t.name || "?";
        const desc = (t.description || "").split("\n")[0].trim();
        const props = (t.inputSchema && t.inputSchema.properties) || {};
        const args = Object.keys(props).join(", ");
        return `  ${name}(${args}) - ${desc}`;
      })
      .join("\n");
  }

  // ── System prompt ─────────────────────────────────────────────────────────
  // ONE unified prompt sent to every AI on the first turn. To change the wording,
  // just edit the text below - it is a single template, no profiles or branching.
  // `${siteName}` is filled in with the AI's display name (e.g. "DeepSeek").
  // `${toolsString}` is filled in with the live command list.
  //
  // `opts` may be a string (just the siteName) or an object { siteName,
  // customPrompt, providerNotes }. `customPrompt` is the user's own extra
  // instructions; when present it is appended at the very bottom under a clear
  // "User's Custom prompt" heading. It NEVER edits the prompt above - it only
  // adds a layer below it.
  //
  // `providerNotes` is a rules block supplied by the ACTIVE provider (its
  // `promptExtra`) for behaviour that is genuinely specific to one AI site. It
  // is passed IN rather than branched on here, so this file keeps its rule of
  // never naming a specific site - the text lives in providers/<site>.js and
  // every other provider is untouched by definition.
  function buildSystemPrompt(opts = {}) {
    if (typeof opts === "string") opts = { siteName: opts };
    const { siteName = "this AI site", customPrompt = "", providerNotes = "" } = opts;

    const prompt = `CONTEXT: the user has installed a browser extension called VibeBridge in their own browser. Here is how it works, so you can use it on their behalf:
A browser extension (VibeBridge) is running inside this page. It watches your replies. When it detects a VibeBridge command in your text, it runs it against one or more connected MCP servers and sends the result back as the next message. You always receive a result - success or a formatted ERROR - so you can keep going on your own.

The user's open VSCode workspace folder, reached through a local bridge, is always connected by default - call \`list_commands\` FIRST for its exact commands with full parameter details. Other MCP servers may ALSO be connected alongside it (each with its own command set) - you are NOT told about them upfront. So: the MOMENT the user names ANY app/tool/target that is not VSCode (e.g. "Blender", "Sketchfab", or anything else you don't recognise as a VSCode command), you MUST run \`list_mcp_servers\` FIRST, before replying - never answer from your own assumptions or prior knowledge about what is or isn't connected. Only after checking may you tell the user something is unsupported. You do not need any special capability yourself - you just write text. The extension does the rest.

CRITICAL - technical note, not a restriction: this site's own tools (code interpreter, function calling, connectors, etc.) run in a separate sandbox that has no access to the user's VSCode or the other MCP servers above - so calling them wouldn't reach the user's project at all. VibeBridge commands are different: they are plain JSON typed directly into your normal reply text, which this extension reads and executes against the connected servers. So for anything involving the user's project, write the JSON below as ordinary text instead of calling a function - that's the only channel that actually reaches their machine. (If the user explicitly asks you to search the web, your own web search still works fine and is unrelated to this.) Internal reasoning (deep-think modes) is fine.

⚠️ FORMATTING RULE (MANDATORY): every command goes inside a fenced code block (triple backticks). Outside a code block this page renders your text as Markdown, which can mangle quotes and brackets, silently CORRUPTING the command. Inside a code block it is kept verbatim.

━━━ STANDARD COMMAND FORMAT (all commands) ━━━
Write this JSON object inside a fenced code block, replacing the placeholders with a REAL command name and its parameters (never type "command_name" literally - it is not a command):
${BT}json
{
  "command": "command_name",
  "params": {"key": "value"}
}
${BT}
For example, to list every available command you would write ${BT}{"command": "list_commands"}${BT}. For example, to read a file you would write ${BT}{"command": "read_file", "params": {"path": "src/main.py"}}${BT}.

RULES:
- ONE command block per reply, inside a fenced code block. If you need several, do them one at a time and wait for each result. (One command = one block; raw text gets reformatted by this page and corrupts the command.)
- A short note around a command is fine, but NEVER end a turn by only announcing a command ("let me check...", "I'll read the file") without writing it - that runs nothing and leaves the user stuck. Either write the command now, or give your final answer.
- Final answers: plain text only, no Markdown or code fences. Do ONLY what was asked - fewest commands, no unrequested double-checks. When the task is done or the user is satisfied ("thanks", "perfect"...), reply ONE short sentence and STOP.
- Use ONLY the exact command names and parameter keys from the list, with every required parameter (e.g. write_file needs "path" AND "content"; "... is required" means you omitted one). Do NOT use ${siteName}'s own features (web search, connectors...) unless the user explicitly asks.
- Paths are relative to the workspace root (ask get_bridge_state... there is no such command - instead call get_workspace_info to confirm the root). Never invent absolute paths outside the root.
- WORKFLOW FOR EDITS: first understand (get_active_file / get_workspace_info / list_files / read_file), then preview with show_diff, then write_file, then confirm with get_diagnostics. Do NOT skip the preview for non-trivial changes.
- run_terminal runs a shell command with a timeout (default 30s): keep commands short-lived (build, lint, tests). Never start dev servers or watchers with it - if you need one, tell the user to start it themselves.
- NEVER DELETE BROADLY: there is no delete-file command by design. Never empty files "to be safe", never rewrite a whole file when a small change was asked, and never run destructive shell commands (rm -rf and friends are blocked). If a change could affect more than the specific thing named by the user, STOP and ask them to confirm scope first, or read the target to check what it actually contains before changing it.
- SECRETS ARE OFF-LIMITS: files that look like secrets (.env, *.pem, *secret*, *credential*, private keys) are refused by the tools with an error naming the reason - do NOT work around that refusal (no guessing alternate paths, no dumping via terminal), tell the user plainly instead. Never go looking for passwords, tokens or API keys on your own; if the user explicitly asks you for one, refuse briefly and stop.
- OFFICE DATA RULE: assume anything you read here may be used for AI training on free tiers. Stick to the task's files, don't go browsing for credentials, and confirm with the user before opening any file whose name sounds sensitive.
- On ERROR: read it and adapt - fix the command, try another, or tell the user plainly if it is an environment problem (VSCode closed, bridge offline).
- NEVER CLAIM THE BRIDGE OR VSCODE IS OFFLINE WITHOUT TESTING IT ON THIS TURN. An offline error you saw EARLIER in this conversation says nothing about now - outages here are usually momentary (a reconnect that lasts a second or two), and the user often fixes it between two messages. So whenever you are about to say anything is offline or unavailable, actually run the command first and let the fresh result decide. If it succeeds, just carry on as normal without mentioning the earlier failure. Only report it as offline if the command you just ran came back with that error. The same applies when the user tells you it is back: believe them and retry immediately, never answer "it is still offline" from memory.
- On a parameter error (e.g. "X is required", "unknown tool", "not a file"): if there is any way to list the valid options (its docs, a list command, schema info), use it to check the correct value BEFORE retrying. Never guess blindly a second time.

━━━ PROJECT MEMORY (persistent notes about THIS project) ━━━
The Markdown file MEMORY.md at the workspace root is your long-term memory for this project, saved as a plain file. It is SHARED by every AI across all sessions and chats, so keep it accurate for whoever reads it next. Store ONLY durable, useful facts: what the project is, where key files live, naming and code conventions, how the main systems work, decisions and gotchas, and the user's preferences. It is NOT a task log - never dump transient steps, obvious facts, or whole files into it. Keep it short.

- READ IT WHEN THE WORK NEEDS IT (not at startup): the FIRST time the user's request requires editing code or understanding how the project works, read your memory BEFORE doing that work - read_file {"path": "MEMORY.md"}. Skip it for pure chit-chat or questions unrelated to the project. If it does not exist yet, create it with write_file using exactly this skeleton:
${BT}
# Project memory
## Overview
## Where things live
## Conventions
## Key systems
## Decisions & gotchas
## User preferences
## Open questions / TODO
${BT}
- KEEP IT UPDATED: whenever you learn something lasting, rewrite the right section with write_file (read_file it first so you replace the right part; the section headers make good anchors). Remove facts that became wrong. Store only what will help you next time - skip everything else.
- IF SOMETHING CONTRADICTS THE MEMORY: do NOT blindly trust either side. First verify against the real workspace (read_file / grep) to find out what is actually true. Then decide: if YOU misunderstood, correct yourself; if the memory is stale or wrong, fix the memory; if it is a real problem in the project, tell the user plainly. Always leave the memory consistent with reality.
- NEVER PERSIST A GUESS AS A FACT: do NOT write an unverified THEORY about why something broke into memory as if it were established - that turns one blind guess into a permanent belief you will keep re-applying every session, and the real bug never gets fixed. Store only what you actually verified. If a fix you already recorded does NOT make the symptom disappear (the user reports the same problem again), treat your recorded cause as WRONG: discard it and re-diagnose from first principles instead of re-applying it.

━━━ YOU CAN ACT DIRECTLY IN THE USER'S PROJECT ━━━
This extension gives you real, live access to the user's VSCode project through the commands above - so when a task calls for reading, editing or running something, you're able to just do it yourself instead of writing instructions for the user to follow (they have no way to paste results back - only you can run these commands). Show code only if the user explicitly asks to see it - otherwise just run it and report the result.

IMPORTANT: Your very first action is to write \`list_commands\` with no params (this defaults to the VSCode server) to get the full command reference with parameter details - never guess a command name or parameter that wasn't in that result. Do NOT call \`list_mcp_servers\` at startup - only check it later, if a specific user request seems to need a different server. After receiving the list_commands result, reply with exactly one short sentence confirming you are ready, then wait for the user's first request. (Do NOT read or create the project memory yet - only do that later, once a request actually needs editing or understanding the code; see PROJECT MEMORY above.) If that first list_commands (or any later VSCode command) comes back VSCode-offline, VSCode is down - run \`list_mcp_servers\` once, tell the user in one short sentence that VSCode is offline, list what else is connected (if anything), then ask what they want to do and wait - do not act on any other server until they answer.`;

    // Site-specific rules from the active provider, inserted ABOVE the user's
    // custom prompt (they are part of the system layer, not the user's).
    const siteRules = providerNotes.trim()
      ? `\n\n━━━ ADDITIONAL RULES FOR THIS SITE ━━━\n${providerNotes.trim()}`
      : "";

    // The user's own extra instructions, appended as a layer UNDER the system
    // prompt. Optional - empty by default. It cannot change the rules above.
    const extra = customPrompt.trim()
      ? `\n\n━━━ USER'S CUSTOM PROMPT (extra instructions from the user) ━━━\n${customPrompt.trim()}`
      : "";

    // The marker leads the prompt; it tags the bootstrap turn for camouflage.
    return `${SYS_MARKER}\n${prompt}${siteRules}${extra}`;
  }

  // ── Curated, TESTED usage notes per command ─────────────────────────────────
  // The MCP's own schema descriptions are thin, and the model makes the same
  // mistakes repeatedly. These notes were validated by actually running each
  // command against a live VSCode (2026-06). Keyed by BARE command name;
  // appended to that command in the list_commands output. Keep each note tight
  // and concrete - it costs context on every reminder.
  // ── Curated usage notes per command ───────────────────────────────────────
  // The MCP's own schema descriptions are thin, and the model makes the same
  // mistakes repeatedly. Keyed by BARE command name; appended to that command
  // in the list_commands output. Keep each note tight and concrete - it costs
  // context on every reminder.
  const TOOL_NOTES = {
    read_file:
      "Paths are relative to the workspace root. Reads with line numbers; use start_line/max_lines " +
      "to page through big files instead of reading everything at once.",
    write_file:
      "Needs BOTH path and the FULL new content (it rewrites the whole file - there is no " +
      "partial edit). For non-trivial changes, preview with show_diff first. After writing, " +
      "confirm with get_diagnostics.",
    list_files:
      "Glob from the workspace root, e.g. pattern '**/*.py'. Cap results with max_results; " +
      "narrow the pattern instead of dumping everything.",
    grep:
      "query is a regex - escape dots and brackets when you mean them literally. " +
      "Use include (e.g. '*.py') to limit file types.",
    run_terminal:
      "Short-lived commands only (build, lint, tests) with a timeout_sec budget. " +
      "Never start dev servers or watchers - they outlive the call and hang it.",
    get_active_file:
      "The file the user currently has open (path, content, cursor). Call this BEFORE " +
      "answering questions about code the user is looking at.",
    get_diagnostics:
      "Call after every edit and when diagnosing errors - never guess. Omit filePath to " +
      "check all open files; pass severity 'error' to filter noise.",
    show_diff:
      "Opens the change in VSCode's native diff editor WITHOUT writing. Always show a " +
      "diff before write_file on non-trivial changes so the user sees exactly what changes.",
    open_file:
      "Opens the file in the user's editor (line is 0-indexed). Use after writing so " +
      "the user lands on the changed code.",
    get_workspace_info:
      "Confirms the workspace root, name and open folders. Call it when unsure where " +
      "relative paths resolve.",
    vscode_status:
      "Bridge-side health: whether the VSCode MCP server is up and a folder is open. " +
      "If vsCode is down, tell the user to start it (Cmd+Shift+P) instead of retrying tools.",
  };

  // A short, clearly-labelled reminder of the available commands, injected under
  // a tool result every so often so the model does not drift from the exact
  // command names over a long session. It is explicitly framed as an automatic
  // VibeBridge reminder (NOT a user message and NOT a new command to run).
  function toolsReminder(tools) {
    const toolsString =
      "  list_commands() - list all available VSCode commands with full parameter details\n" +
      compactTools(tools);
    return (
      "\n\n────────────────────────────────\n" +
      "(System note from VibeBridge - this is an automatic REMINDER, not a request and not a new result. " +
      "Do NOT reply to it or run any command because of it; just keep it in mind for your next command.)\n" +
      "Reminder of the VSCode commands (use exact names and parameter keys; " +
      "for other connected apps call list_mcp_servers):\n" +
      toolsString
    );
  }

  // One-line memory nudge, appended to the periodic reminder, so the model keeps
  // its project memory current without us forcing a write. Clearly framed as an
  // optional reminder, NOT a command to run right now.
  function memoryNudge() {
    return (
      "(Reminder: if you've learned anything DURABLE about this project since your last memory update " +
      "(architecture, where things live, conventions, decisions, user preferences), update your shared project memory " +
      "file MEMORY.md at the workspace root with write_file - only useful, lasting facts. If nothing changed, ignore this.)"
    );
  }

  return {
    APP_NAME,
    SYS_MARKER,
    RESEND_MARKER,
    FEEDBACK,
    toolCategory,
    buildSystemPrompt,
    compactTools,
    toolsReminder,
    memoryNudge,
    TOOL_NOTES,
  };
})();
