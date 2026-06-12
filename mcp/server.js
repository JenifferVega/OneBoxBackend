#!/usr/bin/env node
/**
 * MCP server para probar el agente OneBox de forma interactiva (modo debug).
 *
 * Herramientas disponibles:
 *   onebox_chat     — envía un mensaje y mantiene historial multi-turno
 *   onebox_reset    — reinicia una sesión
 *   onebox_history  — muestra el historial de una sesión
 *   onebox_report   — genera reporte de feedback con análisis y sugerencias
 *   onebox_export   — guarda el reporte en un archivo .md
 *
 * Instalación: npm install @modelcontextprotocol/sdk node-fetch
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Configuración ──────────────────────────────────────────────────────────────
const BASE_URL    = process.env.ONEBOX_BASE_URL    ?? "http://localhost:8000";
const USER_ID     = process.env.ONEBOX_USER_ID     ?? "debug-user-001";
const USER_EMAIL  = process.env.ONEBOX_USER_EMAIL  ?? "debug@onebox.com";
const REPORTS_DIR = process.env.ONEBOX_REPORTS_DIR ?? path.join(__dirname, "reports");
const DEFAULT_SESSION = "default";

// ── Estado en memoria ──────────────────────────────────────────────────────────
// { session_id: { history: [], turns_meta: [] } }
const sessions = {};

function getSession(sessionId) {
  if (!sessions[sessionId]) {
    sessions[sessionId] = { history: [], turns_meta: [] };
  }
  return sessions[sessionId];
}

// ── Server MCP ─────────────────────────────────────────────────────────────────
const server = new Server(
  { name: "onebox-chat", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// ══════════════════════════════════════════════════════════════════════════════
// LISTA DE HERRAMIENTAS
// ══════════════════════════════════════════════════════════════════════════════

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "onebox_chat",
      description:
        "Envía un mensaje al agente OneBox en modo debug y recibe su respuesta. " +
        "El historial se mantiene automáticamente por sesión. " +
        "Usa siempre el mismo session_id en una misma conversación.",
      inputSchema: {
        type: "object",
        properties: {
          message: { type: "string", description: "Mensaje a enviar al agente" },
          session_id: {
            type: "string",
            description: "ID de sesión (usa el mismo en todos los turnos)",
            default: DEFAULT_SESSION,
          },
        },
        required: ["message"],
      },
    },
    {
      name: "onebox_reset",
      description: "Reinicia el historial de una sesión para empezar conversación nueva.",
      inputSchema: {
        type: "object",
        properties: {
          session_id: { type: "string", default: DEFAULT_SESSION },
        },
      },
    },
    {
      name: "onebox_history",
      description: "Muestra el historial de mensajes de una sesión.",
      inputSchema: {
        type: "object",
        properties: {
          session_id: { type: "string", default: DEFAULT_SESSION },
        },
      },
    },
    {
      name: "onebox_report",
      description:
        "Genera un reporte de feedback de la sesión: conversación, análisis por turno " +
        "(iteraciones del planner, herramientas usadas, errores de validación) " +
        "y sugerencias para mejorar catalog.py.",
      inputSchema: {
        type: "object",
        properties: {
          session_id: { type: "string", default: DEFAULT_SESSION },
        },
      },
    },
    {
      name: "onebox_export",
      description: "Guarda el reporte como archivo .md en mcp/reports/.",
      inputSchema: {
        type: "object",
        properties: {
          session_id: { type: "string", default: DEFAULT_SESSION },
          filename: {
            type: "string",
            description: "Nombre del archivo (sin extensión).",
          },
        },
      },
    },
    {
      name: "onebox_from_text_preview",
      description:
        "Llama a POST /api/text/analyze con un texto pegado (conversación WhatsApp, correo, notas). " +
        "Devuelve el preview del agente en modo debug: participantes detectados, tareas con assigned_to " +
        "y fechas, sin crear nada en la base de datos. Útil para verificar la calidad del análisis.",
      inputSchema: {
        type: "object",
        properties: {
          text: { type: "string", description: "Texto o conversación a analizar" },
          source: {
            type: "string",
            description: "Origen del texto: 'whatsapp', 'email', 'notes', etc.",
            default: "whatsapp",
          },
        },
        required: ["text"],
      },
    },
    {
      name: "onebox_from_text_create",
      description:
        "Llama a POST /api/projects/from-text con un texto pegado. " +
        "Crea el proyecto REAL en la base de datos usando el agente completo: " +
        "detecta participantes, crea tareas con assigned_to y fechas. " +
        "Retorna el projectId creado y la respuesta del agente.",
      inputSchema: {
        type: "object",
        properties: {
          text: { type: "string", description: "Texto o conversación desde la que crear el proyecto" },
          name: {
            type: "string",
            description: "Nombre del proyecto (opcional, el agente lo infiere si no se da)",
          },
        },
        required: ["text"],
      },
    },
    {
      name: "onebox_list_projects",
      description:
        "Lista los proyectos del usuario. Útil para obtener projectIds antes de llamar " +
        "onebox_analyze_text_in_project.",
      inputSchema: {
        type: "object",
        properties: {},
        required: [],
      },
    },
    {
      name: "onebox_analyze_text_in_project",
      description:
        "Llama a POST /api/projects/{project_id}/analyze-text. " +
        "Genera insights (tareas con assigned_to, riesgos, decisiones) dentro de un proyecto " +
        "existente usando generate_insights_for_project con la lista real de participantes. " +
        "Útil para verificar que las tareas quedan asignadas a los participantes correctos.",
      inputSchema: {
        type: "object",
        properties: {
          project_id: { type: "string", description: "ID del proyecto existente (ej: proj-abc123)" },
          text: { type: "string", description: "Texto o conversación a analizar dentro del proyecto" },
          source: {
            type: "string",
            description: "Origen del texto: 'whatsapp', 'email', 'notes', etc.",
            default: "whatsapp",
          },
        },
        required: ["project_id", "text"],
      },
    },
  ],
}));

// ══════════════════════════════════════════════════════════════════════════════
// DISPATCH DE HERRAMIENTAS
// ══════════════════════════════════════════════════════════════════════════════

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args = {} } = request.params;

  switch (name) {
    case "onebox_chat":              return handleChat(args);
    case "onebox_reset":             return handleReset(args);
    case "onebox_history":           return handleHistory(args);
    case "onebox_report":            return handleReport(args);
    case "onebox_export":            return handleExport(args);
    case "onebox_from_text_preview":        return handleFromTextPreview(args);
    case "onebox_from_text_create":         return handleFromTextCreate(args);
    case "onebox_list_projects":            return handleListProjects(args);
    case "onebox_analyze_text_in_project":  return handleAnalyzeTextInProject(args);
    default:
      return text(`Herramienta desconocida: ${name}`);
  }
});

// ══════════════════════════════════════════════════════════════════════════════
// HANDLERS
// ══════════════════════════════════════════════════════════════════════════════

async function handleChat(args) {
  const message   = (args.message || "").trim();
  const sessionId = args.session_id || DEFAULT_SESSION;

  if (!message) return text("⚠️ El mensaje no puede estar vacío.");

  const session = getSession(sessionId);
  const { history } = session;

  const payload = { message, history, debug: true, session_id: sessionId };
  const headers = {
    "x-user-id":    USER_ID,
    "x-user-email": USER_EMAIL,
    "Content-Type": "application/json",
  };

  let data;
  try {
    const res = await fetch(`${BASE_URL}/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.text();
      return text(`❌ Error HTTP ${res.status}: ${body.slice(0, 400)}`);
    }
    data = await res.json();
  } catch (err) {
    if (err.code === "ECONNREFUSED") {
      return text(
        `❌ No se pudo conectar a ${BASE_URL}\n¿Está corriendo el servidor? → \`uvicorn main:app --reload\``
      );
    }
    return text(`❌ Error inesperado: ${err.message}`);
  }

  const agentResponse = data.response ?? "";
  const toolsUsed     = data.toolsUsed ?? [];
  const debugInfo     = data.debug_info ?? {};

  // Acumular historial
  history.push({ role: "user",      content: message });
  history.push({ role: "assistant", content: agentResponse });

  // Guardar metadata del turno
  session.turns_meta.push({
    turn:       session.turns_meta.length + 1,
    message,
    response:   agentResponse,
    tools_used: toolsUsed,
    debug_info: debugInfo,
    timestamp:  new Date().toISOString(),
  });

  // Formatear respuesta
  const lines = [`🤖 **Agente:** ${agentResponse}`];

  if (toolsUsed.length) {
    lines.push(`\n🔧 **Herramientas:** \`${toolsUsed.join("`, `")}\``);
  }

  const decision  = debugInfo.planner_decision ?? "";
  const iteration = debugInfo.iteration ?? 1;
  const plan      = debugInfo.plan ?? [];

  if (decision) {
    const iterWarn = iteration > 1 ? " ⚠️" : "";
    lines.push(`📊 **Planner:** \`${decision}\` | iteraciones: **${iteration}**${iterWarn}`);
  }

  if (plan.length) {
    const steps = plan.map((s) => `  ${s.step}. \`${s.tool}\``).join("\n");
    lines.push(`📋 **Plan:**\n${steps}`);
  }

  const turnNum = history.length / 2;
  lines.push(`\n*(turno ${turnNum} · sesión \`${sessionId}\` · debug=true)*`);

  return text(lines.join("\n"));
}

async function handleReset(args) {
  const sessionId = args.session_id || DEFAULT_SESSION;
  const prev = sessions[sessionId] ?? {};
  const prevTurns = (prev.turns_meta ?? []).length;
  sessions[sessionId] = { history: [], turns_meta: [] };

  // Limpiar cache dry-run en el servidor
  try {
    await fetch(`${BASE_URL}/debug/cache/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
      headers: { "x-user-id": USER_ID, "x-user-email": USER_EMAIL },
    });
  } catch (_) {
    // Si el servidor no está corriendo, ignorar silenciosamente
  }

  return text(`✅ Sesión \`${sessionId}\` reiniciada. (${prevTurns} turnos anteriores borrados)`);
}

function handleHistory(args) {
  const sessionId = args.session_id || DEFAULT_SESSION;
  const session   = sessions[sessionId] ?? {};
  const history   = session.history ?? [];

  if (!history.length) {
    return text(`La sesión \`${sessionId}\` no tiene historial aún.`);
  }

  const lines = [`📜 **Historial · sesión \`${sessionId}\`** (${history.length / 2} turnos)\n`];
  for (const msg of history) {
    const icon    = msg.role === "user" ? "👤" : "🤖";
    const content = (msg.content ?? "").slice(0, 500);
    lines.push(`${icon} ${content}\n`);
  }
  return text(lines.join("\n"));
}

function handleReport(args) {
  const sessionId = args.session_id || DEFAULT_SESSION;
  return text(buildReport(sessionId));
}

function handleExport(args) {
  const sessionId = args.session_id || DEFAULT_SESSION;
  let filename    = args.filename
    ?? `report_${sessionId}_${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}`;

  if (!filename.endsWith(".md")) filename += ".md";

  fs.mkdirSync(REPORTS_DIR, { recursive: true });
  const outputPath = path.join(REPORTS_DIR, filename);
  const reportMd   = buildReport(sessionId);
  fs.writeFileSync(outputPath, reportMd, "utf-8");

  return text(`✅ Reporte guardado en:\n\`${outputPath}\``);
}

// ══════════════════════════════════════════════════════════════════════════════
// GENERACIÓN DE REPORTE
// ══════════════════════════════════════════════════════════════════════════════

function buildReport(sessionId) {
  const session   = sessions[sessionId] ?? {};
  const turnsMeta = session.turns_meta ?? [];

  if (!turnsMeta.length) {
    return `⚠️ La sesión \`${sessionId}\` no tiene turnos registrados.`;
  }

  const now = new Date().toLocaleString("es-MX");
  const lines = [
    `# Reporte de Feedback — OneBox Agent`,
    `**Sesión:** \`${sessionId}\` · **Fecha:** ${now} · **Turnos:** ${turnsMeta.length}`,
    "",
  ];

  // 1. Conversación completa
  lines.push("## 1. Conversación completa", "");
  for (const meta of turnsMeta) {
    lines.push(
      `### Turno ${meta.turn}`,
      `**👤 Usuario:** ${meta.message}`,
      "",
      `**🤖 Agente:** ${meta.response}`,
      ""
    );
  }

  // 2. Análisis por turno
  lines.push("---", "## 2. Análisis por turno", "");
  const issuesFound = [];

  for (const meta of turnsMeta) {
    const di        = meta.debug_info ?? {};
    const tools     = meta.tools_used ?? [];
    const iteration = di.iteration ?? 1;
    const decision  = di.planner_decision ?? "N/A";
    const plan      = di.plan ?? [];
    const simCalls  = di.simulated_calls ?? [];

    lines.push(`#### Turno ${meta.turn}: _${meta.message.slice(0, 80)}_`);
    lines.push(`- **Decisión del planner:** \`${decision}\``);
    lines.push(
      `- **Iteraciones:** ${iteration}` + (iteration > 1 ? " ⚠️ múltiples iteraciones" : "")
    );
    lines.push(
      `- **Herramientas ejecutadas:** ${tools.length ? tools.map((t) => `\`${t}\``).join(", ") : "ninguna"}`
    );

    if (plan.length) {
      lines.push(`- **Plan generado:** ${plan.map((s) => `\`${s.tool}\``).join(" → ")}`);
    }

    // Errores de validación
    const valErrors = simCalls.filter(
      (c) => c?.simulated_result?._validation_error
    );
    for (const ve of valErrors) {
      const errMsg = (ve.simulated_result.error ?? "").slice(0, 200);
      lines.push(`- **❌ Error de validación paso ${ve.step} (\`${ve.tool}\`):** ${errMsg}`);
      issuesFound.push({ turn: meta.turn, type: "validation_error", tool: ve.tool, detail: errMsg });
    }

    if (iteration > 1) {
      issuesFound.push({
        turn: meta.turn,
        type: "high_iterations",
        detail: `El planner necesitó ${iteration} iteraciones para: '${meta.message.slice(0, 60)}'`,
      });
    }

    lines.push("");
  }

  // 3. Problemas detectados
  lines.push("---", "## 3. Problemas detectados", "");
  if (!issuesFound.length) {
    lines.push("✅ No se detectaron problemas en esta sesión.");
  } else {
    for (const issue of issuesFound) {
      if (issue.type === "high_iterations") {
        lines.push(`- ⚠️ **Turno ${issue.turn} — Múltiples iteraciones:** ${issue.detail}`);
      } else if (issue.type === "validation_error") {
        lines.push(`- ❌ **Turno ${issue.turn} — Validación fallida en \`${issue.tool}\`:** ${issue.detail}`);
      }
    }
  }
  lines.push("");

  // 4. Sugerencias para catalog.py
  lines.push("---", "## 4. Sugerencias de mejora al catalog.py", "");
  const suggestions = generateCatalogSuggestions(issuesFound, turnsMeta);
  if (!suggestions.length) {
    lines.push("✅ No se generaron sugerencias — la sesión fue correcta.");
  } else {
    for (const s of suggestions) {
      lines.push(`### ${s.title}`, s.body, "");
    }
  }

  // 5. Datos para entrenamiento
  lines.push("---", "## 5. Datos para entrenamiento (JSON)", "", "```json");
  const trainingData = turnsMeta.map((m) => ({
    turn:              m.turn,
    message:           m.message,
    response:          m.response,
    tools_used:        m.tools_used,
    planner_decision:  m.debug_info?.planner_decision ?? null,
    iterations:        m.debug_info?.iteration ?? 1,
    plan:              m.debug_info?.plan ?? [],
  }));
  lines.push(JSON.stringify(trainingData, null, 2), "```");

  return lines.join("\n");
}

function generateCatalogSuggestions(issues, turnsMeta) {
  const suggestions = [];
  const seen = new Set();

  for (const issue of issues) {
    const key = `${issue.type}:${issue.tool ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);

    if (issue.type === "high_iterations") {
      const turnData = turnsMeta.find((m) => m.turn === issue.turn) ?? {};
      suggestions.push({
        title: `⚠️ Turno ${issue.turn}: El planner tardó múltiples iteraciones`,
        body:
          `**Mensaje:** _${turnData.message ?? ""}_\n\n` +
          "**Posible causa:** La regla en `REQUIRED_PARAMS` no es suficientemente explícita.\n\n" +
          "**Acción sugerida:** Agregar un ejemplo ❌/✅ en la sección correspondiente de `catalog.py`.",
      });
    } else if (issue.type === "validation_error") {
      suggestions.push({
        title: `❌ Validación fallida en \`${issue.tool}\` — reforzar ejemplo en catalog`,
        body:
          `**Error detectado:** ${issue.detail.slice(0, 200)}\n\n` +
          `**Acción sugerida:** En \`MULTISTEP_RECIPES\` o \`REQUIRED_PARAMS\`, ` +
          `agregar un ejemplo explícito de cómo resolver el parámetro inválido en \`${issue.tool}\`. ` +
          `Usar el patrón ❌ INCORRECTO / ✅ CORRECTO.`,
      });
    }
  }

  return suggestions;
}

async function handleFromTextPreview(args) {
  const textInput = (args.text || "").trim();
  const source    = args.source || "whatsapp";

  if (!textInput) return text("⚠️ El texto no puede estar vacío.");

  const headers = {
    "x-user-id":    USER_ID,
    "x-user-email": USER_EMAIL,
    "Content-Type": "application/json",
  };

  let data;
  try {
    const res = await fetch(`${BASE_URL}/api/text/analyze`, {
      method: "POST",
      headers,
      body: JSON.stringify({ text: textInput, source }),
    });
    if (!res.ok) {
      const body = await res.text();
      return text(`❌ Error HTTP ${res.status}: ${body.slice(0, 400)}`);
    }
    data = await res.json();
  } catch (err) {
    if (err.code === "ECONNREFUSED") {
      return text(`❌ No se pudo conectar a ${BASE_URL}. ¿Está corriendo el servidor?`);
    }
    return text(`❌ Error: ${err.message}`);
  }

  const suggestion   = data.suggestion ?? {};
  const participants = suggestion.detected_participants ?? [];
  const tasks        = suggestion.tasks ?? [];

  const lines = [
    `## 📋 Preview del proyecto (sin guardar)`,
    `**Nombre:** ${suggestion.name || "—"}`,
    `**Tipo:** ${suggestion.type || "—"}`,
    `**Descripción:** ${(suggestion.description || "—").slice(0, 300)}`,
    `**Draft ID:** \`${data.draftId || "—"}\``,
    "",
  ];

  if (participants.length) {
    lines.push(`### 👥 Participantes detectados (${participants.length}):`);
    for (const p of participants) {
      const email = p.email ? ` 📧 ${p.email}` : "";
      lines.push(`  • **${p.name || "?"}** — ${p.role || ""}${email}`);
    }
  } else {
    lines.push("👥 No se detectaron participantes.");
  }

  lines.push("");

  if (tasks.length) {
    lines.push(`### ✅ Tareas detectadas (${tasks.length}):`);
    for (const t of tasks) {
      const assigned = t.assigned_to ? ` → **${t.assigned_to}**` : "";
      const due      = t.due_date    ? ` (hasta ${t.due_date})`   : "";
      lines.push(`  • ${t.text || "?"}${assigned}${due}`);
    }
  } else {
    lines.push("✅ No se detectaron tareas.");
  }

  if (data.agentResponse) {
    lines.push("", `🤖 **Respuesta del agente:** ${data.agentResponse.slice(0, 400)}`);
  }

  return text(lines.join("\n"));
}

async function handleFromTextCreate(args) {
  const textInput = (args.text || "").trim();
  const name      = (args.name || "").trim();

  if (!textInput) return text("⚠️ El texto no puede estar vacío.");

  const headers = {
    "x-user-id":    USER_ID,
    "x-user-email": USER_EMAIL,
    "Content-Type": "application/json",
  };

  let data;
  try {
    const res = await fetch(`${BASE_URL}/api/projects/from-text`, {
      method: "POST",
      headers,
      body: JSON.stringify({ text: textInput, name: name || null, channels: ["WhatsApp"], source: "whatsapp" }),
    });
    if (!res.ok) {
      const body = await res.text();
      return text(`❌ Error HTTP ${res.status}: ${body.slice(0, 400)}`);
    }
    data = await res.json();
  } catch (err) {
    if (err.code === "ECONNREFUSED") {
      return text(`❌ No se pudo conectar a ${BASE_URL}. ¿Está corriendo el servidor?`);
    }
    return text(`❌ Error: ${err.message}`);
  }

  const lines = [
    `## ✅ Proyecto creado`,
    `**Nombre:** ${data.name || "—"}`,
    `**Project ID:** \`${data.projectId || "—"}\``,
    `**Herramientas usadas:** ${(data.tools_used || []).join(", ") || "—"}`,
    "",
    `🤖 **Respuesta del agente:**`,
    data.response || "—",
  ];

  return text(lines.join("\n"));
}

async function handleListProjects(args) {
  const headers = { "x-user-id": USER_ID, "x-user-email": USER_EMAIL };
  let data;
  try {
    const res = await fetch(`${BASE_URL}/api/projects`, { headers });
    if (!res.ok) return text(`❌ Error HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    return text(`❌ Error: ${err.message}`);
  }

  const projects = Array.isArray(data) ? data : (data.projects || []);
  if (!projects.length) return text("No hay proyectos.");

  const lines = ["## 📁 Proyectos", ""];
  for (const p of projects.slice(0, 20)) {
    const participants = (p.participants || []).map(x => x.nombre || x.name).filter(Boolean).join(", ");
    lines.push(`- **${p.name}** — \`${p.projectId}\``);
    if (participants) lines.push(`  👥 ${participants}`);
  }
  return text(lines.join("\n"));
}

async function handleAnalyzeTextInProject(args) {
  const projectId = (args.project_id || "").trim();
  const textInput = (args.text || "").trim();
  const source    = args.source || "whatsapp";

  if (!projectId) return text("⚠️ project_id es requerido.");
  if (!textInput)  return text("⚠️ El texto no puede estar vacío.");

  const headers = {
    "x-user-id":    USER_ID,
    "x-user-email": USER_EMAIL,
    "Content-Type": "application/json",
  };

  let data;
  try {
    const res = await fetch(`${BASE_URL}/api/projects/${projectId}/analyze-text`, {
      method: "POST",
      headers,
      body: JSON.stringify({ text: textInput, source }),
    });
    if (!res.ok) {
      const body = await res.text();
      return text(`❌ Error HTTP ${res.status}: ${body.slice(0, 400)}`);
    }
    data = await res.json();
  } catch (err) {
    if (err.code === "ECONNREFUSED") return text(`❌ No se pudo conectar a ${BASE_URL}. ¿Está corriendo el servidor?`);
    return text(`❌ Error: ${err.message}`);
  }

  const insights = data.insightsGenerated || {};
  const analysis = insights.analysis || {};

  const lines = [
    `## 🔍 Insights generados para \`${projectId}\``,
    `**Total insights:** ${insights.count || 0}`,
    "",
  ];

  if (analysis.summary) {
    lines.push(`### 📝 Resumen`, analysis.summary, "");
  }

  const tasks = analysis.tasks || [];
  if (tasks.length) {
    lines.push(`### ✅ Tareas (${tasks.length})`);
    for (const t of tasks) {
      const assigned = t.assigned_to ? ` → **${t.assigned_to}**` : "";
      const due      = t.due_date    ? ` (hasta ${t.due_date})` : "";
      lines.push(`  • ${t.text}${assigned}${due}`);
    }
    lines.push("");
  }

  const blockers = analysis.blockers || [];
  if (blockers.length) {
    lines.push(`### 🚫 Bloqueos`);
    for (const b of blockers) {
      const assigned = b.assigned_to ? ` → ${b.assigned_to}` : "";
      lines.push(`  • ${b.text || b}${assigned}`);
    }
    lines.push("");
  }

  if ((analysis.risks || []).length) {
    lines.push(`### ⚠️ Riesgos`);
    for (const r of analysis.risks) lines.push(`  • ${r}`);
    lines.push("");
  }

  if ((analysis.decisions || []).length) {
    lines.push(`### 🎯 Decisiones`);
    for (const d of analysis.decisions) lines.push(`  • ${d}`);
  }

  return text(lines.join("\n"));
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function text(content) {
  return { content: [{ type: "text", text: String(content) }] };
}

// ══════════════════════════════════════════════════════════════════════════════
// ENTRY POINT
// ══════════════════════════════════════════════════════════════════════════════

const transport = new StdioServerTransport();
await server.connect(transport);
