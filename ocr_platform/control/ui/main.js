import { createJsonRequester } from "./api.js";
import { clearSessionToken, loadSessionToken, saveSessionToken } from "./auth.js";
import { createDiagnosticsModule } from "./diagnostics.js";
import { ui } from "./dom.js";
import { createJobOperationsModule, createJobWizard } from "./job-wizard.js";
import { createJobsModule } from "./jobs.js";
import { createNavigation } from "./navigation.js";
import { createProfilesModule } from "./profiles.js";
import { createRemoteAdminModule } from "./remote-admin.js";
import { state } from "./state.js";
import { createEventsModule } from "./ui-events.js";
import { createHelpersModule } from "./ui-helpers.js";
import { createWorkersModule } from "./workers.js";

const REFRESH_MS = 3000;
const requestJson = createJsonRequester(() => ui.controlApiToken.value.trim());
const app = { state, ui, requestJson, clearSessionToken, saveSessionToken, jobWizard: null, navigation: null };

Object.assign(app, createHelpersModule(app));
Object.assign(app, createProfilesModule(app));
Object.assign(app, createDiagnosticsModule(app));
Object.assign(app, createRemoteAdminModule(app));
Object.assign(app, createWorkersModule(app));
Object.assign(app, createJobsModule(app));
Object.assign(app, createJobOperationsModule(app));
Object.assign(app, createEventsModule(app));

async function init() {
  ui.controlApiToken.value = loadSessionToken();
  await app.loadModelProfiles();
  app.syncExecutionModeFields();
  app.renderPreflight(null);
  app.navigation = createNavigation({
    onRouteChange: (route) => {
      if (route !== "jobs/new") app.jobWizard?.clearSecret();
    },
  });
  app.navigation.init();
  app.jobWizard = createJobWizard({ onCancel: () => app.navigation.navigate("jobs") });
  app.jobWizard.init();
  app.bindEvents();
  await app.refreshOperationsData({ quiet: !ui.controlApiToken.value.trim() });
  app.loadRemoteWorkerTargets().catch(() => {});
  state.refreshTimer = setInterval(async () => {
    await app.refreshOperationsData().catch((error) => {
      app.setMessage(ui.jobsMessage, String(error.message || error), true);
    });
  }, REFRESH_MS);
}

window.addEventListener("beforeunload", () => {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  if (state.tokenRefreshTimer) clearTimeout(state.tokenRefreshTimer);
});

init();
