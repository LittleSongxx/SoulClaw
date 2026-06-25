<script setup lang="ts">
import {
  Activity,
  AlertTriangle,
  BookOpen,
  Brain,
  CheckCircle2,
  Database,
  FileText,
  Globe2,
  KeyRound,
  LogOut,
  Play,
  PlugZap,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Timer,
  Wrench
} from "@lucide/vue";
import { computed, onMounted, ref } from "vue";

type ViewKey =
  | "dashboard"
  | "wiki"
  | "memory"
  | "skills"
  | "tools"
  | "cron"
  | "mcp"
  | "gateways"
  | "proposals"
  | "approvals"
  | "runs"
  | "audit"
  | "settings";

const navItems: Array<{ key: ViewKey; label: string; icon: unknown }> = [
  { key: "dashboard", label: "Dashboard", icon: Activity },
  { key: "wiki", label: "Wiki", icon: BookOpen },
  { key: "memory", label: "Memory", icon: Brain },
  { key: "skills", label: "Skills", icon: Wrench },
  { key: "tools", label: "Tools", icon: PlugZap },
  { key: "cron", label: "Cron", icon: Timer },
  { key: "mcp", label: "MCP", icon: PlugZap },
  { key: "gateways", label: "Gateways", icon: Globe2 },
  { key: "proposals", label: "Proposals", icon: CheckCircle2 },
  { key: "approvals", label: "Approvals", icon: ShieldCheck },
  { key: "runs", label: "Runs", icon: Play },
  { key: "audit", label: "Audit", icon: FileText },
  { key: "settings", label: "Settings", icon: Settings }
];

const token = ref(localStorage.getItem("zlagent_token") || "");
const active = ref<ViewKey>("dashboard");
const username = ref("admin");
const password = ref("zlagent-admin");
const loading = ref(false);
const error = ref("");
const searchQuery = ref("");
const turnMessage = ref("");
const newMemory = ref("");

const me = ref<Record<string, unknown> | null>(null);
const settingsData = ref<Record<string, unknown>>({});
const wikiHealth = ref<Record<string, unknown>>({});
const wikiPages = ref<Array<Record<string, unknown>>>([]);
const wikiSearchResults = ref<Array<Record<string, unknown>>>([]);
const wikiErrors = ref<Array<Record<string, unknown>>>([]);
const memories = ref<Array<Record<string, unknown>>>([]);
const skills = ref<Array<Record<string, unknown>>>([]);
const tools = ref<Array<Record<string, unknown>>>([]);
const cronJobs = ref<Array<Record<string, unknown>>>([]);
const mcpServers = ref<Array<Record<string, unknown>>>([]);
const gateways = ref<Array<Record<string, unknown>>>([]);
const proposals = ref<Array<Record<string, unknown>>>([]);
const approvals = ref<Array<Record<string, unknown>>>([]);
const runs = ref<Array<Record<string, unknown>>>([]);
const events = ref<Array<Record<string, unknown>>>([]);
const audit = ref<Array<Record<string, unknown>>>([]);
const lastTurn = ref<Record<string, unknown> | null>(null);

const loggedIn = computed(() => Boolean(token.value));
const pageTitle = computed(() => navItems.find((item) => item.key === active.value)?.label || "Dashboard");
const qdrantLabel = computed(() => {
  if (!settingsData.value.qdrant_enabled) return "Off";
  return settingsData.value.qdrant_available ? "Available" : "Degraded";
});
const dreamLabel = computed(() => {
  if (!settingsData.value.dream_review_enabled) return "Off";
  return settingsData.value.dream_review_job_enabled ? "Scheduled" : "Not scheduled";
});
const displayedWikiItems = computed(() => {
  const items = wikiSearchResults.value.length ? wikiSearchResults.value : wikiPages.value;
  return items.map((item) => {
    const page = isRecord(item.page) ? item.page : item;
    return {
      page_key: String(page.page_key || ""),
      title: String(page.title || ""),
      summary: String(page.summary || ""),
      source: String(item.source || "")
    };
  });
});

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

async function api(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("Content-Type", "application/json");
  if (token.value) headers.set("Authorization", `Bearer ${token.value}`);
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401) {
    logout();
    throw new Error("Authentication expired");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.error || response.statusText);
  return payload;
}

async function login() {
  loading.value = true;
  error.value = "";
  try {
    const payload = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: username.value, password: password.value })
    });
    token.value = payload.access_token;
    localStorage.setItem("zlagent_token", token.value);
    await refreshAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function logout() {
  token.value = "";
  localStorage.removeItem("zlagent_token");
  me.value = null;
}

async function refreshAll() {
  if (!loggedIn.value) return;
  await Promise.all([loadMe(), loadSettings(), loadDashboard(), loadActive()]);
}

async function loadMe() {
  me.value = await api("/api/auth/me");
}

async function loadSettings() {
  settingsData.value = await api("/api/settings");
}

async function loadDashboard() {
  const [health, runtime] = await Promise.all([api("/api/wiki/health"), api("/api/events?limit=10")]);
  wikiHealth.value = health;
  events.value = runtime.items || [];
}

async function loadActive() {
  if (!loggedIn.value) return;
  if (active.value === "wiki") {
    const [pages, errorsPayload, health] = await Promise.all([
      api("/api/wiki/pages"),
      api("/api/wiki/errors"),
      api("/api/wiki/health")
    ]);
    wikiPages.value = pages.items || [];
    wikiErrors.value = errorsPayload.items || [];
    wikiHealth.value = health;
  }
  if (active.value === "memory") memories.value = (await api("/api/memory")).items || [];
  if (active.value === "skills") skills.value = (await api("/api/skills")).items || [];
  if (active.value === "tools") tools.value = (await api("/api/tools")).items || [];
  if (active.value === "cron") cronJobs.value = (await api("/api/cron")).items || [];
  if (active.value === "mcp") mcpServers.value = (await api("/api/mcp")).items || [];
  if (active.value === "gateways") gateways.value = (await api("/api/gateways")).items || [];
  if (active.value === "proposals") proposals.value = (await api("/api/skills/proposals")).items || [];
  if (active.value === "approvals") approvals.value = (await api("/api/approvals")).items || [];
  if (active.value === "runs") runs.value = (await api("/api/runs")).items || [];
  if (active.value === "audit") {
    audit.value = (await api("/api/audit")).items || [];
    events.value = (await api("/api/events")).items || [];
  }
  if (active.value === "settings") await loadSettings();
}

async function setView(key: ViewKey) {
  active.value = key;
  await loadActive();
}

async function compileWiki() {
  await api("/api/wiki/compile", { method: "POST" });
  await loadActive();
  await loadDashboard();
}

async function searchWiki() {
  const payload = await api("/api/wiki/search", {
    method: "POST",
    body: JSON.stringify({ query: searchQuery.value, limit: 10 })
  });
  wikiSearchResults.value = payload.items || [];
}

async function createMemory() {
  if (!newMemory.value.trim()) return;
  await api("/api/memory", {
    method: "POST",
    body: JSON.stringify({
      kind: "agent_note",
      content: newMemory.value,
      source: "admin",
      importance: 0.55,
      confidence: 0.65,
      stability: 0.55
    })
  });
  newMemory.value = "";
  await loadActive();
}

async function scanSkills() {
  await api("/api/skills/scan", { method: "POST" });
  await loadActive();
}

async function runDream() {
  await api("/api/dream/run", {
    method: "POST",
    body: JSON.stringify({ window_hours: 24, limit: 50 })
  });
  await loadActive();
  await loadDashboard();
}

async function runTurn() {
  if (!turnMessage.value.trim()) return;
  lastTurn.value = await api("/api/runs/turn", {
    method: "POST",
    body: JSON.stringify({ message: turnMessage.value, session_id: "console" })
  });
  await loadDashboard();
}

function fmt(value: unknown) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

onMounted(refreshAll);
</script>

<template>
  <main v-if="!loggedIn" class="login-shell">
    <section class="login-panel">
      <div class="brand-row">
        <Database :size="28" />
        <div>
          <h1>ZLAgent</h1>
          <p>Platform Console</p>
        </div>
      </div>
      <form @submit.prevent="login" class="login-form">
        <label>
          <span>Username</span>
          <input v-model="username" autocomplete="username" />
        </label>
        <label>
          <span>Password</span>
          <input v-model="password" type="password" autocomplete="current-password" />
        </label>
        <button class="primary" type="submit" :disabled="loading">
          <KeyRound :size="18" />
          <span>{{ loading ? "Signing in" : "Sign in" }}</span>
        </button>
      </form>
      <p v-if="error" class="error">{{ error }}</p>
    </section>
  </main>

  <main v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand-row compact">
        <Database :size="24" />
        <div>
          <h1>ZLAgent</h1>
          <p>{{ me?.username || "admin" }}</p>
        </div>
      </div>
      <nav>
        <button v-for="item in navItems" :key="item.key" :class="{ active: active === item.key }" @click="setView(item.key)">
          <component :is="item.icon" :size="18" />
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <button class="ghost logout" @click="logout">
        <LogOut :size="18" />
        <span>Sign out</span>
      </button>
    </aside>

    <section class="content">
      <header class="topbar">
        <div>
          <h2>{{ pageTitle }}</h2>
          <p>{{ fmt(settingsData.environment) }}</p>
        </div>
        <button class="icon-button" @click="loadActive" title="Refresh">
          <RefreshCw :size="18" />
        </button>
      </header>

      <section v-if="active === 'dashboard'" class="stack">
        <div class="metrics">
          <article>
            <span>Wiki Pages</span>
            <strong>{{ wikiHealth.pages ?? 0 }}</strong>
          </article>
          <article>
            <span>Open Errors</span>
            <strong>{{ wikiHealth.open_errors ?? 0 }}</strong>
          </article>
          <article>
            <span>Qdrant</span>
            <strong>{{ qdrantLabel }}</strong>
          </article>
          <article>
            <span>Dream Review</span>
            <strong>{{ dreamLabel }}</strong>
          </article>
        </div>
        <section class="tool-surface">
          <textarea v-model="turnMessage" placeholder="Run a console turn"></textarea>
          <button class="primary" @click="runTurn">
            <Play :size="18" />
            <span>Run</span>
          </button>
        </section>
        <pre v-if="lastTurn">{{ fmt(lastTurn) }}</pre>
        <section class="list">
          <article v-for="event in events" :key="String(event.id)">
            <strong>{{ event.event_type }}</strong>
            <span>{{ event.severity }}</span>
            <p>{{ fmt(event.payload) }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'wiki'" class="stack">
        <div class="actions">
          <button class="primary" @click="compileWiki">
            <RefreshCw :size="18" />
            <span>Compile</span>
          </button>
          <div class="searchbox">
            <Search :size="18" />
            <input v-model="searchQuery" @keyup.enter="searchWiki" placeholder="Search wiki" />
          </div>
          <button class="secondary" @click="searchWiki">Search</button>
        </div>
        <section v-if="wikiErrors.length" class="alert-list">
          <article v-for="item in wikiErrors" :key="String(item.id)">
            <AlertTriangle :size="18" />
            <div>
              <strong>{{ item.error_type }} · {{ item.page_key }}</strong>
              <p>{{ item.root_cause }}</p>
            </div>
          </article>
        </section>
        <section class="list">
          <article v-for="item in displayedWikiItems" :key="item.page_key">
            <strong>{{ item.title }}</strong>
            <span>{{ item.page_key }}</span>
            <p>{{ item.summary }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'memory'" class="stack">
        <section class="tool-surface">
          <textarea v-model="newMemory" placeholder="Add memory"></textarea>
          <button class="primary" @click="createMemory">
            <Brain :size="18" />
            <span>Add</span>
          </button>
        </section>
        <section class="list">
          <article v-for="item in memories" :key="String(item.id)">
            <strong>{{ item.kind }}</strong>
            <span>importance {{ item.importance }} · confidence {{ item.confidence }}</span>
            <p>{{ item.content }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'skills'" class="stack">
        <button class="primary fit" @click="scanSkills">
          <RefreshCw :size="18" />
          <span>Scan</span>
        </button>
        <section class="list">
          <article v-for="item in skills" :key="String(item.id)">
            <strong>{{ item.name }}</strong>
            <span>{{ item.skill_key }} · {{ item.status }}</span>
            <p>{{ item.description }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'tools'" class="list">
        <article v-for="item in tools" :key="String(item.name)">
          <strong>{{ item.name }}</strong>
          <span>{{ item.scope }} · {{ item.available ? "available" : "unavailable" }}</span>
          <p>{{ item.description }}</p>
        </article>
      </section>

      <section v-if="active === 'cron'" class="list">
        <article v-for="item in cronJobs" :key="String(item.id)">
          <strong>{{ item.name }}</strong>
          <span>{{ item.cron_expr }} · {{ item.timezone }} · {{ item.enabled ? "enabled" : "disabled" }}</span>
          <p>{{ item.instruction }}</p>
        </article>
      </section>

      <section v-if="active === 'mcp'" class="list">
        <article v-for="item in mcpServers" :key="String(item.id)">
          <strong>{{ item.name }}</strong>
          <span>{{ item.transport }} · {{ item.enabled ? "enabled" : "disabled" }} · {{ item.status }} · {{ item.tool_count || 0 }} tools</span>
          <p>{{ item.command || item.url }}</p>
          <small v-if="item.last_error">{{ item.last_error }}</small>
        </article>
      </section>

      <section v-if="active === 'gateways'" class="list">
        <article v-for="item in gateways" :key="String(item.id)">
          <strong>{{ item.name }}</strong>
          <span>{{ item.kind }} · {{ item.status }} · in {{ item.inbound_count || 0 }} · out {{ item.outbound_count || 0 }}</span>
          <p>{{ item.endpoint }}</p>
          <small v-if="item.last_error">{{ item.last_error }}</small>
        </article>
      </section>

      <section v-if="active === 'proposals'" class="stack">
        <div class="actions">
          <button class="primary" @click="runDream">
            <RefreshCw :size="18" />
            <span>Run Dream</span>
          </button>
        </div>
        <div class="list">
          <article v-for="item in proposals" :key="String(item.id)">
            <strong>{{ item.action }} · {{ item.target_type }}</strong>
            <span>{{ item.status }} · {{ item.risk_level }}</span>
            <pre>{{ fmt(item.result || item.payload) }}</pre>
          </article>
        </div>
      </section>

      <section v-if="active === 'approvals'" class="list">
        <article v-for="item in approvals" :key="String(item.id)">
          <strong>{{ item.subject_type }}</strong>
          <span>{{ item.status }}</span>
          <pre>{{ fmt(item.payload) }}</pre>
        </article>
      </section>

      <section v-if="active === 'runs'" class="list">
        <article v-for="item in runs" :key="String(item.id)">
          <strong>{{ item.tool_name }}</strong>
          <span>{{ item.status }} · {{ item.turn_id }}</span>
          <pre>{{ fmt(item.result) }}</pre>
        </article>
      </section>

      <section v-if="active === 'audit'" class="stack">
        <section class="list">
          <article v-for="item in audit" :key="String(item.id)">
            <strong>{{ item.action }}</strong>
            <span>{{ item.target_type }} · {{ item.created_at }}</span>
            <pre>{{ fmt(item.payload) }}</pre>
          </article>
        </section>
      </section>

      <section v-if="active === 'settings'" class="settings-grid">
        <article v-for="(value, key) in settingsData" :key="key">
          <span>{{ key }}</span>
          <strong>{{ fmt(value) }}</strong>
        </article>
      </section>
    </section>
  </main>
</template>
