"use strict";

const $ = (id) => document.getElementById(id);
const state = { mode: "login", user: null, sessionId: null, busy: false, selection: 0 };

async function api(route, body, method = "POST") {
  const options = { method, headers: { "X-Super-Bot-Web": "1" }, credentials: "same-origin" };
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch("/api/" + route, options);
  let data = {};
  try { data = await response.json(); } catch (_) { /* non-JSON error */ }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? "Please check the values you entered." : data.detail;
    const error = new Error(detail || "Request failed (" + response.status + ")");
    error.status = response.status;
    throw error;
  }
  return data;
}

function say(id, text, ok = false) {
  const element = $(id);
  element.textContent = text || "";
  element.classList.toggle("ok", ok);
}

function setMode(mode) {
  state.mode = mode;
  for (const name of ["login", "register", "recover"]) $("tab-" + name).classList.toggle("active", name === mode);
  $("display-row").hidden = mode !== "register";
  $("code-row").hidden = mode !== "recover";
  $("password-label").textContent = mode === "recover" ? "New password" : "Password";
  $("password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("auth-submit").textContent = { login: "Sign in", register: "Create account", recover: "Reset password" }[mode];
  say("auth-message", "");
}

function showAuth() {
  state.selection++;
  state.user = null;
  state.sessionId = null;
  $("app-view").hidden = true;
  if ($("settings").open) $("settings").close();
  $("auth-view").hidden = false;
  $("password").value = "";
}

function showApp(user) {
  state.user = user;
  $("who").textContent = user.display_name || user.username;
  $("account-id").textContent = "Account ID: " + user.user_id;
  $("auth-view").hidden = true;
  $("app-view").hidden = false;
  refreshChats();
}

async function boot() {
  try {
    const me = await api("me", null, "GET");
    showApp({ user_id: me.user_id, username: me.username, display_name: me.profile.display_name });
  } catch (_) {
    showAuth();
  }
}

$("tab-login").onclick = () => setMode("login");
$("tab-register").onclick = () => setMode("register");
$("tab-recover").onclick = () => setMode("recover");

$("auth-form").onsubmit = async (event) => {
  event.preventDefault();
  const username = $("username").value.trim();
  const password = $("password").value;
  try {
    if (state.mode === "recover") {
      await api("recover", { username, recovery_code: $("recovery-code").value.trim(), new_password: password });
      setMode("login");
      say("auth-message", "Password reset. Sign in with your new password.", true);
      $("password").value = "";
      return;
    }
    const body = { username, password };
    if (state.mode === "register") body.display_name = $("display-name").value.trim();
    const grant = await api(state.mode, body);
    showApp({ user_id: grant.user_id, username: grant.username, display_name: grant.display_name });
  } catch (error) {
    say("auth-message", error.message);
  }
  $("password").value = "";
};

function parseLog(log) {
  const messages = [];
  for (const line of log.split("\n")) {
    const match = /^\[[^\]]+\]\s*([^:\n]{1,100}):\s?(.*)$/.exec(line);
    if (match) messages.push({ speaker: match[1], text: match[2] });
    else if (messages.length) messages[messages.length - 1].text += "\n" + line;
  }
  return messages;
}

function chatTitle(chat) {
  const last = parseLog(chat.preview || "").pop();
  const text = last ? last.text.trim() : "";
  return text ? text.slice(0, 60) : "Chat " + chat.session_id;
}

function addBubble(text, mine, pending = false) {
  const bubble = document.createElement("div");
  bubble.className = "bubble" + (mine ? " me" : "") + (pending ? " pending" : "");
  bubble.textContent = text;
  $("messages").appendChild(bubble);
  $("messages").scrollTop = $("messages").scrollHeight;
  return bubble;
}

function renderLog(log) {
  $("messages").replaceChildren();
  for (const message of parseLog(log)) addBubble(message.text, message.speaker !== "AI");
}

function setComposer(enabled) {
  $("message").disabled = !enabled || state.busy;
  $("send").disabled = !enabled || state.busy;
}

async function refreshChats() {
  try {
    const result = await api("chats", { query: $("search").value });
    const list = $("chat-list");
    list.replaceChildren();
    for (const chat of result.chats) {
      const item = document.createElement("li");
      item.classList.toggle("active", chat.session_id === state.sessionId);
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = chatTitle(chat);
      const remove = document.createElement("button");
      remove.className = "remove";
      remove.textContent = "✕";
      remove.title = "Delete chat";
      remove.onclick = async (event) => {
        event.stopPropagation();
        if (!confirm("Delete this chat permanently?")) return;
        try {
          await api("chats/delete", { session_id: chat.session_id });
          if (state.sessionId === chat.session_id) { state.sessionId = null; $("messages").replaceChildren(); setComposer(false); }
          refreshChats();
        } catch (error) { say("chat-message", error.message); }
      };
      item.append(title, remove);
      item.onclick = () => openChat(chat.session_id);
      list.appendChild(item);
    }
  } catch (error) {
    if (error.status === 401) showAuth(); else say("chat-message", error.message);
  }
}

async function openChat(sessionId) {
  const selection = ++state.selection;
  try {
    const chat = await api("chats/get", { session_id: sessionId });
    if (selection !== state.selection) return;
    state.sessionId = sessionId;
    renderLog(chat.chat_log);
    setComposer(true);
    say("chat-message", "");
    refreshChats();
  } catch (error) { say("chat-message", error.message); }
}

$("new-chat").onclick = async () => {
  const selection = ++state.selection;
  say("chat-message", "Starting chat…", true);
  try {
    const session = await api("sessions", {});
    if (selection !== state.selection) return;
    state.sessionId = session.session_ID;
    renderLog(session.Chat_log);
    setComposer(true);
    say("chat-message", "");
    refreshChats();
  } catch (error) { say("chat-message", error.message); }
};

$("search").oninput = () => refreshChats();

$("send-form").onsubmit = async (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  if (!text || state.sessionId === null || state.busy) return;
  state.busy = true;
  setComposer(true);
  $("message").value = "";
  addBubble(text, true);
  const waiting = addBubble("Thinking…", false, true);
  const sessionId = state.sessionId;
  const selection = state.selection;
  try {
    const result = await api("send", { session_id: sessionId, message: text });
    if (sessionId === state.sessionId && selection === state.selection) renderLog(result.Chat_log);
    say("chat-message", "");
  } catch (error) {
    waiting.remove();
    say("chat-message", error.status === 401 ? "Your session ended. Please sign in again." : error.message);
    if (error.status === 401) showAuth();
  } finally {
    state.busy = false;
    setComposer(state.sessionId !== null);
    $("message").focus();
    refreshChats();
  }
};

$("open-settings").onclick = () => { $("profile-name").value = state.user.display_name || ""; say("settings-message", ""); $("settings").showModal(); };
$("close-settings").onclick = () => $("settings").close();

async function refreshKnowledge() {
  try {
    const result = await api("knowledge", null, "GET");
    const list = $("knowledge-list");
    list.replaceChildren();
    for (const source of result.sources) {
      const item = document.createElement("li");
      const text = document.createElement("span");
      text.textContent = source.title + " (" + source.category + ")";
      const remove = document.createElement("button");
      remove.className = "remove";
      remove.textContent = "Remove";
      remove.onclick = async () => {
        if (!confirm("Remove this knowledge source?")) return;
        try {
          await api("knowledge/delete", { source_id: source.id });
          refreshKnowledge();
        } catch (error) { say("knowledge-message", error.message); }
      };
      item.append(text, remove);
      list.appendChild(item);
    }
    if (!result.sources.length) {
      const item = document.createElement("li");
      item.textContent = "No sources indexed for this account.";
      list.appendChild(item);
    }
    say("knowledge-message", "");
  } catch (error) {
    say("knowledge-message", error.status === 401 ? "Your session ended. Please sign in again." : error.message);
    if (error.status === 401) showAuth();
  }
}

$("open-knowledge").onclick = () => { $("knowledge").showModal(); refreshKnowledge(); };
$("close-knowledge").onclick = () => $("knowledge").close();
$("refresh-knowledge").onclick = refreshKnowledge;

$("profile-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const profile = await api("profile", { display_name: $("profile-name").value.trim() });
    state.user.display_name = profile.display_name;
    $("who").textContent = profile.display_name || state.user.username;
    say("settings-message", "Profile saved.", true);
  } catch (error) { say("settings-message", error.message); }
};

$("password-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    await api("password", { current_password: $("current-password").value, new_password: $("new-password").value });
    say("settings-message", "Password changed. Other devices were signed out.", true);
  } catch (error) { say("settings-message", error.message); }
  $("current-password").value = $("new-password").value = "";
};

$("recovery-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const result = await api("recovery_code", { password: $("recovery-password").value });
    $("recovery-output").textContent = result.recovery_code;
    say("settings-message", result.note, true);
  } catch (error) { say("settings-message", error.message); }
  $("recovery-password").value = "";
};

async function signOut(route) {
  try { await api(route, {}); } catch (_) { /* already signed out */ }
  showAuth();
}
$("logout").onclick = () => signOut("logout");
$("logout-all").onclick = () => signOut("logout_all");

$("delete-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!confirm("Permanently delete your account and every chat?")) return;
  try {
    await api("delete", { password: $("delete-password").value });
    showAuth();
  } catch (error) { say("settings-message", error.message); }
  $("delete-password").value = "";
};

setMode("login");
boot();

// Release temporary server sessions when this browser document leaves.
window.addEventListener("pagehide", () => {
  fetch("/api/leave", { method: "POST", credentials: "same-origin", keepalive: true,
    headers: { "X-Super-Bot-Web": "1", "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
});
