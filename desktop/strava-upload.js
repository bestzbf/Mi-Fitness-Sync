(() => {
  if (window.xiaomiRunUpload) return;
  const trusted = () => location.protocol === "https:" &&
    ["strava.com", "www.strava.com"].includes(location.hostname);
  const visible = element => element && element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden";
  const activityID = href => {
    try {
      const url = new URL(href, location.href);
      if (url.protocol !== "https:" || !["strava.com", "www.strava.com"].includes(url.hostname)) return null;
      const match = url.pathname.match(/^\/activities\/(\d+)\/?$/);
      const id = match && Number(match[1]);
      return Number.isSafeInteger(id) && id > 0 ? id : null;
    } catch (_) { return null; }
  };
  const state = { selected: false, saveClicked: false, activityID: null };
  const models = new WeakMap();
  let observingModels = false;
  const positiveID = value => {
    if (typeof value !== "number" && !(typeof value === "string" && /^\d+$/.test(value))) return null;
    const id = Number(value);
    return Number.isSafeInteger(id) && id > 0 ? id : null;
  };
  function observeModels() {
    if (observingModels) return;
    const uploads = window.Strava && window.Strava.Uploads;
    const prototype = uploads && uploads.UploadActivityView && uploads.UploadActivityView.prototype;
    if (!prototype || typeof prototype.render !== "function" || typeof uploads.Activity !== "function") return;
    const render = prototype.render;
    // Observe the model belonging to this exact upload row. Current Strava templates
    // can omit .activity-link; the official save handler uses this model's ID.
    prototype.render = function (...args) {
      const result = render.apply(this, args);
      try {
        const list = document.querySelector("#uploadProgress .uploads");
        if (state.selected && list && list.contains(this.el) && this.model instanceof uploads.Activity) {
          models.set(this.el, { id: positiveID(this.model.get("id")) });
        }
      } catch (_) { /* Observation must not change the official page's behavior. */ }
      return result;
    };
    observingModels = true;
  }
  const errorDetail = element => (element ? element.textContent : "")
    .replace(/https?:\/\/\S+/gi, "")
    .replace(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi, "[邮箱]")
    .replace(/\S+\.(?:fit|gpx|tcx)\b/gi, "[活动文件]")
    .replace(/\s+/g, " ").trim().slice(0, 240);
  const rows = () => Array.from(document.querySelectorAll("#uploadProgress .uploads > li"));
  const input = () => document.querySelector('#uploadFile input[type="file"][name="files[]"]');
  const saveButton = () => Array.from(document.querySelectorAll("#uploadFooter .save-and-view"))
    .find(button => visible(button) && !button.disabled && !button.classList.contains("disabled"));
  function inspect() {
    if (!trusted()) return { status: "untrusted" };
    if (/^\/login(?:\/|$)|^\/session(?:\/|$)/.test(location.pathname)) return { status: "login" };
    if (document.querySelector('#challenge-form, iframe[src*="challenges.cloudflare.com"]')) return { status: "login" };
    if (!location.pathname.startsWith("/upload")) return { status: "other_page" };
    observeModels();
    for (const selector of [".email-must-be-verified", ".rate-limited", ".server-error", ".unsupported-file-type", ".saving-error"]) {
      const error = Array.from(document.querySelectorAll("#uploadFile " + selector + ", #uploadFooter " + selector)).find(visible);
      if (error) {
        return { status: "error", reason: selector.slice(1), detail: errorDetail(error) };
      }
    }
    const list = rows();
    if (list.length > 1) return { status: "error", reason: "unexpected-activities" };
    if (list.length === 1) {
      if (!state.selected) return { status: "error", reason: "previous-upload-unfinished" };
      const row = list[0];
      if (row.classList.contains("error")) {
        const message = row.querySelector(".error-message");
        const text = message ? message.textContent : "";
        // Strava renders its duplicate_url inside this row's error-message.
        // Use that official link so the check works in every page language.
        const link = message && message.querySelector('a[href]');
        let id = link && activityID(link.href);
        if (!id) {
          const match = text.match(/duplicate (?:of )?activity\s+(\d+)/i);
          if (match && Number.isSafeInteger(Number(match[1]))) id = Number(match[1]);
        }
        if (id > 0) return { status: "duplicate", activity_id: id };
        return { status: "error", reason: "file-rejected", detail: errorDetail(message) };
      }
      if (!row.classList.contains("finished")) return { status: "processing" };
      const link = row.querySelector(".activity-link a[href]");
      const linkID = link && activityID(link.href);
      const modelID = models.get(row)?.id;
      if (linkID && modelID && linkID !== modelID) return { status: "error", reason: "conflicting-activity-id" };
      const id = modelID || linkID;
      if (!id) return { status: "processing", reason: "waiting-activity-id" };
      const selection = row.querySelector(".sport-type .selection");
      const sport = selection && window.jQuery && window.jQuery(selection).data("current");
      if (!["Run", "TrailRun", "VirtualRun"].includes(sport)) return { status: "error", reason: "not-running" };
      if (state.saveClicked) return { status: "saving", activity_id: state.activityID };
      return saveButton() ? { status: "ready_to_save", activity_id: id } : { status: "processing" };
    }
    const file = input();
    if (file && file.form && new URL(file.form.action).pathname === "/upload/files" &&
        document.readyState !== "loading" && window.jQuery && window.Strava && window.Strava.Uploads &&
        window.Strava.Uploads.FileUploadView) {
      return { status: state.selected ? "processing" : "ready_for_file" };
    }
    return { status: "loading" };
  }
  window.xiaomiRunUpload = {
    inspect,
    selectFile(payload) {
      if (state.selected) return { status: "already_selected" };
      if (inspect().status !== "ready_for_file") return { status: "not_ready" };
      if (typeof payload.name !== "string" || !/^run_\d{8}_\d{6}_[a-f0-9]{16}\.fit$/.test(payload.name)) {
        return { status: "error", reason: "invalid-file" };
      }
      const bytes = Uint8Array.from(atob(payload.data), character => character.charCodeAt(0));
      if (!bytes.length || bytes.length > 25 * 1024 * 1024) return { status: "error", reason: "invalid-file" };
      const transfer = new DataTransfer();
      transfer.items.add(new File([bytes], payload.name, { type: "application/octet-stream" }));
      const file = input();
      state.selected = true;
      file.files = transfer.files;
      file.dispatchEvent(new Event("change", { bubbles: true }));
      return { status: "selected" };
    },
    save() {
      if (state.saveClicked) return { status: "already_saving", activity_id: state.activityID };
      const result = inspect();
      if (result.status !== "ready_to_save") return result;
      const button = saveButton();
      if (!button) return { status: "processing" };
      state.saveClicked = true;
      state.activityID = result.activity_id;
      button.click();
      return { status: "saving", activity_id: state.activityID };
    }
  };
})()
