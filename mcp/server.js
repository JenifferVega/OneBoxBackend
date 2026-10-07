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
      name: "onebox_onboarding_dryrun",
      description:
        "DRY-RUN del análisis de onboarding: llama a POST /api/text/analyze-insights-dryrun. " +
        "Corre la IA REAL sobre el TEXTO COMPLETO para ver la profundidad de los insights, " +
        "pero NO crea proyecto ni escribe NADA en DynamoDB. Ideal para verificar la calidad " +
        "de la IA sin ensuciar datos. Devuelve summary, tipo real, perfil del cliente, insight " +
        "clave y las listas de tareas/trabajo hecho/bloqueos/riesgos/decisiones/métricas/problemas técnicos.",
      inputSchema: {
        type: "object",
        properties: {
          text: { type: "string", description: "Texto/transcript a analizar (conversación, brief, acta)" },
        },
        required: ["text"],
      },
    },
    // ── Grafo de planificación (agent/planning) ─────────────────────────────
    {
      name: "planning_list_cases",
      description: "Lista los casos de prueba del grafo de planificación (tests/planning/cases) y el resultado de su última corrida.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "planning_run_case",
      description:
        "Corre un caso del grafo de planificación con la IA REAL: before.json + input.json -> propuesta -> after.json, " +
        "y lo califica contra expected.json. Tarda 1-3 minutos. Devuelve el reporte.",
      inputSchema: {
        type: "object",
        properties: { case: { type: "string", description: "Carpeta del caso, p. ej. farmacia_reanalyze" } },
        required: ["case"],
      },
    },
    {
      name: "planning_report",
      description: "Muestra el último report.md de un caso sin volver a correrlo.",
      inputSchema: { type: "object", properties: { case: { type: "string" } }, required: ["case"] },
    },
    {
      name: "planning_chat",
      description:
        "Envía un mensaje al CHAT DE PLANIFICACIÓN del proyecto de un caso (dry-run: el estado vive en " +
        "tests/planning/sessions, nunca en DynamoDB). Preguntas ('¿qué bloquea el lanzamiento?'), cambios " +
        "('cierra todo lo de accesos de Meta'), leer un documento, o decidir sobre la propuesta abierta " +
        "('aprueba todo', 'aprueba 1,3', 'rechaza'). message='--reset' reinicia la sesión.",
      inputSchema: {
        type: "object",
        properties: {
          case: { type: "string" },
          message: { type: "string" },
          session_id: { type: "string", default: "default" },
        },
        required: ["case", "message"],
      },
    },
    // ── Conversaciones guionadas (tests/planning/conversations) ──────────────
    {
      name: "planning_list_conversations",
      description:
        "Lista las conversaciones de prueba (tests/planning/conversations) con su tipo: member (un miembro da " +
        "su update en el chat de la app), consolidation (varios updates + Trello -> resumen y propuesta para el " +
        "dueño, aprobación reflejada en Trello) y replan (el dueño replanifica con contexto). Título, pasos y " +
        "resultado de la última corrida.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "planning_run_conversation",
      description:
        "Corre una conversación guionada (member / consolidation / replan) con la IA REAL (tablas de AWS " +
        "simuladas en memoria, Trello desde fixtures con la forma real de la API, documentos con el extractor " +
        "real) y la califica turno por turno. ~1-2 min por turno: devuelve 'sigue corriendo' y luego se consulta " +
        "con planning_conversation_report. upto=N corre solo los primeros N turnos; dry=true no llama a la IA " +
        "(solo prueba la plomería, sin costo).",
      inputSchema: {
        type: "object",
        properties: {
          name: { type: "string", description: "Carpeta, p. ej. member_jenny, consolidate_onebox, bion_replan_mensaje" },
          upto: { type: "number", description: "Solo los primeros N turnos (0 = todos)" },
          dry: { type: "boolean", description: "Sin IA: solo plomería" },
        },
        required: ["name"],
      },
    },
    {
      name: "planning_conversation_report",
      description: "Muestra el último reporte (turno por turno) de una conversación, o si sigue corriendo.",
      inputSchema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
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
    case "onebox_from_text_preview": return handleFromTextPreview(args);
    case "onebox_from_text_create":  return handleFromTextCreate(args);
    case "onebox_onboarding_dryrun": return handleOnboardingDryrun(args);
    case "planning_list_cases":      return handlePlanningList();
    case "planning_run_case":        return handlePlanningRun(args);
    case "planning_report":          return handlePlanningReport(args);
    case "planning_chat":            return handlePlanningChat(args);
    case "planning_list_conversations":  return handleConversationList();
    case "planning_run_conversation":    return handleConversationRun(args);
    case "planning_conversation_report": return handleConversationReport(args);
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

async function handleOnboardingDryrun(args) {
  const textInput = (args.text || "").trim();
  if (!textInput) return text("⚠️ 'text' no puede estar vacío.");

  const headers = {
    "x-user-id":    USER_ID,
    "x-user-email": USER_EMAIL,
    "Content-Type": "application/json",
  };

  let data;
  try {
    const res = await fetch(`${BASE_URL}/api/text/analyze-insights-dryrun`, {
      method: "POST",
      headers,
      body: JSON.stringify({ text: textInput }),
    });
    if (!res.ok) {
      const body = await res.text();
      return text(`❌ Error HTTP ${res.status}: ${body.slice(0, 500)}`);
    }
    data = await res.json();
  } catch (err) {
    if (err.code === "ECONNREFUSED") {
      return text(`❌ No se pudo conectar a ${BASE_URL}. ¿Está corriendo el servidor?`);
    }
    return text(`❌ Error: ${err.message}`);
  }

  const a = data.analysis ?? {};
  const txtOf  = (x) => (x && typeof x === "object" ? (x.text ?? "") : String(x));
  const listOf = (key, n = 40) => (a[key] ?? []).slice(0, n).map(txtOf).filter(Boolean);

  const lines = [
    "## 🧪 Onboarding DRY-RUN (IA real, SIN crear datos)",
    `**generated:** ${data.generated} · **insights (simulados):** ${data.insightCount}`,
    "",
    "### 📊 Resumen",
    a.summary || "—",
    "",
    `**🎯 Tipo real:** ${a.project_type_real || "—"}`,
    `**👤 Perfil cliente:** ${a.client_profile || "—"}`,
    `**💡 Insight clave:** ${a.key_insight || "—"}`,
    "",
  ];

  const groups = [
    ["tasks", "✅ Tareas pendientes"],
    ["work_done", "🏁 Trabajo hecho"],
    ["blockers", "🚧 Bloqueos"],
    ["risks", "⚠️ Riesgos"],
    ["decisions", "🧭 Decisiones"],
    ["metrics", "📈 Métricas"],
    ["tech_issues", "🔧 Problemas técnicos"],
  ];
  for (const [key, label] of groups) {
    const vals = listOf(key);
    lines.push(`### ${label} (${vals.length})`);
    for (const v of vals) lines.push(`  • ${v.slice(0, 220)}`);
    lines.push("");
  }
  if (data.reason) lines.push(`⚠️ reason: ${data.reason}`);

  return text(lines.join("\n"));
}

// ══════════════════════════════════════════════════════════════════════════════
// GRAFO DE PLANIFICACIÓN — por HTTP al backend (/debug/planning/*), igual que
// el resto de herramientas. El backend solo responde a peticiones locales.
// ══════════════════════════════════════════════════════════════════════════════

async function planningCall(method, route, body) {
  try {
    const res = await fetch(`${BASE_URL}${route}`, {
      method,
      headers: { "Content-Type": "application/json", "x-user-id": USER_ID, "x-user-email": USER_EMAIL },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return { error: `HTTP ${res.status}: ${data.detail ?? JSON.stringify(data)}` };
    return { data };
  } catch (e) {
    return { error: `No se pudo conectar a ${BASE_URL}. ¿Está corriendo el servidor? (${e.message})` };
  }
}

async function handlePlanningList() {
  const { data, error } = await planningCall("GET", "/debug/planning/cases");
  if (error) return text(`❌ ${error}`);
  return text(["Casos de planificación:", ...data.cases.map((c) =>
    `- ${c.case}: ${c.ready ? "listo" : "FALTA before.json"} | última: ${c.last || "nunca corrido"}`)].join("\n"));
}

async function handlePlanningRun(args) {
  const { data, error } = await planningCall("POST", "/debug/planning/run", { case: args.case || "" });
  if (error) return text(`❌ ${error}`);
  if (data.passed === null || data.passed === undefined) return text(data.report || "⏳ Sin resultado todavía.");
  return text(`${data.passed ? "PASS" : "FAIL"} existing=${data.existing_accuracy} plan=${data.plan_items_recall}` +
    `${(data.violations || []).map((v) => `\n  ! ${v}`).join("")}\n\n${(data.report || "").slice(0, 12000)}`);
}

async function handlePlanningReport(args) {
  const { data, error } = await planningCall("GET", `/debug/planning/report/${encodeURIComponent(args.case || "")}`);
  if (error) return text(`❌ ${error}`);
  return text((data.report || "Sin corridas todavía.").slice(0, 15000));
}

async function handlePlanningChat(args) {
  const { data, error } = await planningCall("POST", "/debug/planning/chat", {
    case: args.case || "", message: args.message || "", session_id: args.session_id || DEFAULT_SESSION });
  if (error) return text(`❌ ${error}`);
  if (data.reset) return text(`Sesión reiniciada: ${data.session}`);
  const lines = [data.reply || "", "",
    `intent: ${data.intent} | aplicado: ${data.applied} | tareas abiertas: ${data.open_tasks} | nodos: ${(data.nodes || []).join(" -> ")}`];
  const p = data.proposal;
  if (p) {
    lines.push("", `PROPUESTA ${JSON.stringify(p.counts)}`, ...(p.operations || []));
    if ((p.questions || []).length) lines.push("", "PREGUNTAS", ...p.questions.map((q, i) => `${i + 1}. ${q}`));
    if ((p.warnings || []).length) lines.push("", "AVISOS", ...p.warnings);
  }
  return text(lines.join("\n"));
}

async function handleConversationList() {
  const { data, error } = await planningCall("GET", "/debug/planning/conversations");
  if (error) return text(`❌ ${error}`);
  return text(["Conversaciones de prueba:", ...data.conversations.map((c) => {
    const last = c.last ? `${c.last.passed}/${c.last.turns} turnos OK (${c.last.seconds} s)` : "nunca corrida";
    return `- ${c.name} [${c.kind || "replan"}] (${c.turns} pasos): ${c.title} | última: ${last}${c.job === "running" ? " | ⏳ corriendo" : ""}`;
  })].join("\n"));
}

async function handleConversationRun(args) {
  const { data, error } = await planningCall("POST", "/debug/planning/conversation/run", {
    name: args.name || "", upto: Number(args.upto || 0), dry: !!args.dry });
  if (error) return text(`❌ ${error}`);
  if (!data.done) return text(data.report || "⏳ Sigue corriendo.");
  const r = data.result || {};
  return text(`${r.passed ?? "?"}/${r.turns ?? "?"} turnos OK\n\n${(data.report || "").slice(0, 15000)}`);
}

async function handleConversationReport(args) {
  const { data, error } = await planningCall("GET",
    `/debug/planning/conversation/report/${encodeURIComponent(args.name || "")}`);
  if (error) return text(`❌ ${error}`);
  return text((data.report || "Sin corridas todavía.").slice(0, 20000));
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
