/* TicketBox — shared frontend helpers (auth, API wrapper, nav, toasts). */
(function () {
  const TOKEN_KEY = "tbs_token";
  const USER_KEY = "tbs_user";

  const getToken = () => localStorage.getItem(TOKEN_KEY);
  const getUser = () => {
    try { return JSON.parse(localStorage.getItem(USER_KEY)); }
    catch { return null; }
  };
  const setSession = (token, user) => {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  };
  const clearSession = () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  };

  /** fetch wrapper: JSON in/out, Bearer token, throws {status, detail}. */
  async function api(path, { method = "GET", body } = {}) {
    const headers = { "Content-Type": "application/json" };
    const token = getToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    const res = await fetch(path, {
      method, headers, body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty body (e.g. images) */ }
    if (!res.ok) {
      if (res.status === 401) clearSession();
      const detail = data && data.detail !== undefined ? data.detail : data;
      throw { status: res.status, detail };
    }
    return data;
  }

  /** Human-readable message out of an API error object. */
  const errMsg = (e) => {
    if (!e) return "Something went wrong";
    if (typeof e.detail === "string") return e.detail;
    if (e.detail && e.detail.message) return e.detail.message;
    return "Something went wrong (" + (e.status || "network") + ")";
  };

  /* ------------------------------ nav ------------------------------------ */
  function renderNav(active) {
    const user = getUser();
    const el = document.getElementById("nav");
    if (!el) return;
    const links = [["index.html", "Events", "events"]];
    if (user && user.role === "customer")
      links.push(["bookings.html", "My bookings", "bookings"]);
    if (user && (user.role === "organiser" || user.role === "admin"))
      links.push(["organiser.html", "Organiser", "organiser"]);
    if (user && user.role === "admin")
      links.push(["admin.html", "Admin", "admin"]);

    el.innerHTML = `
      <a class="brand" href="index.html"><span class="brand-mark">⟡</span>TicketBox</a>
      <div class="nav-links">
        ${links.map(([href, label, key]) =>
          `<a href="${href}" class="${key === active ? "active" : ""}">${label}</a>`).join("")}
      </div>
      <div class="nav-user">
        ${user
          ? `<span class="who mono">${user.name} · ${user.role}</span>
             <button class="btn ghost small" id="logoutBtn">Log out</button>`
          : `<a class="btn ghost small" href="login.html">Log in</a>
             <a class="btn gold small" href="register.html">Sign up</a>`}
      </div>`;
    const out = document.getElementById("logoutBtn");
    if (out) out.onclick = () => { clearSession(); location.href = "index.html"; };
  }

  /* ----------------------------- toasts ---------------------------------- */
  function toast(message, kind = "info") {
    let holder = document.getElementById("toasts");
    if (!holder) {
      holder = document.createElement("div");
      holder.id = "toasts";
      document.body.appendChild(holder);
    }
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = message;
    holder.appendChild(t);
    setTimeout(() => t.classList.add("show"), 20);
    setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); }, 4200);
  }

  /* ---------------------------- formatting -------------------------------- */
  const fmtDate = (iso) => new Date(iso).toLocaleString("en-IN", {
    weekday: "short", day: "numeric", month: "short",
    hour: "numeric", minute: "2-digit",
  });
  const fmtMoney = (n) => "₹" + Number(n).toLocaleString("en-IN");

  /**
   * Server-synced countdown. Uses server_time to cancel client clock drift.
   * Calls onTick("mm:ss", secondsLeft) every second, onDone() at zero.
   */
  function countdown(expiresIso, serverIso, onTick, onDone) {
    const drift = Date.parse(serverIso) - Date.now();
    const timer = setInterval(() => {
      const left = Math.round((Date.parse(expiresIso) - (Date.now() + drift)) / 1000);
      if (left <= 0) { clearInterval(timer); onTick("0:00", 0); onDone && onDone(); return; }
      const m = Math.floor(left / 60), s = String(left % 60).padStart(2, "0");
      onTick(`${m}:${s}`, left);
    }, 1000);
    return () => clearInterval(timer);
  }

  const requireLogin = (nextUrl) => {
    if (!getToken()) {
      location.href = "login.html?next=" + encodeURIComponent(nextUrl || location.pathname + location.search);
      return false;
    }
    return true;
  };

  window.TBS = { api, errMsg, getUser, getToken, setSession, clearSession,
                 renderNav, toast, fmtDate, fmtMoney, countdown, requireLogin };
})();
