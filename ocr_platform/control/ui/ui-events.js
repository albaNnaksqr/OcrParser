

export function createEventsModule(app) {
  const { state, ui, saveSessionToken, clearSessionToken } = app;
  function bindEvents() {
    ui.controlApiToken.addEventListener("input", () => {
      const token = ui.controlApiToken.value.trim();
      if (token) {
        saveSessionToken(token);
        if (state.tokenRefreshTimer) clearTimeout(state.tokenRefreshTimer);
        state.tokenRefreshTimer = setTimeout(() => {
          app.refreshOperationsData().catch((error) => {
            app.setMessage(ui.jobsMessage, String(error.message || error), true);
          });
        }, 250);
      } else {
        clearSessionToken();
      }
    });
    ui.modelProfile.addEventListener("change", app.applyModelProfile);
    document.getElementById("saveModelProfileBtn").addEventListener("click", async () => {
      try {
        await app.saveModelProfile();
      } catch (error) {
        app.setMessage(ui.modelProfileMessage, String(error.message || error), true);
      }
    });
    document.getElementById("refreshModelProfilesBtn").addEventListener("click", async () => {
      await app.loadModelProfiles();
    });
    ui.executionMode.addEventListener("change", () => {
      app.syncExecutionModeFields();
      app.schedulePreflight();
      const mode = app.selectedInputMode();
      if (mode === "directory") {
        app.setMessage(ui.jobMessage, "Directory mode runs on the selected server.");
      } else if (mode === "remote_folder_snapshot") {
        app.setMessage(ui.jobMessage, "Remote folder snapshot lets an eligible server scan the shared path.");
      } else if (mode === "distributed_remote_folder_snapshot") {
        app.setMessage(ui.jobMessage, "Distributed manifest scan lets eligible servers split directory scanning.");
      } else {
        app.setMessage(ui.jobMessage, "Static shard mode uses the eligible server pool.");
      }
    });
    document.getElementById("inputDir").addEventListener("input", app.schedulePreflight);
    document.getElementById("targetFilesPerShard").addEventListener("input", app.schedulePreflight);
    ui.workerScope.addEventListener("change", () => {
      app.syncExecutionModeFields();
      app.schedulePreflight();
    });
    ui.singleWorkerSelect.addEventListener("change", app.schedulePreflight);
    ui.selectedWorkersList.addEventListener("change", app.schedulePreflight);
    ui.manualWorkerIds.addEventListener("input", app.schedulePreflight);
    document.getElementById("registerServerBtn").addEventListener("click", async () => {
      try {
        await app.registerServer();
      } catch (error) {
        app.setMessage(ui.serverMessage, String(error.message || error), true);
      }
    });
    document.getElementById("refreshServersBtn").addEventListener("click", async () => {
      try {
        await app.loadServers();
        app.setMessage(ui.serverMessage, "Server list refreshed");
      } catch (error) {
        app.setMessage(ui.serverMessage, String(error.message || error), true);
      }
    });
    ui.workerFilter?.addEventListener("input", () => app.renderServers(state.servers));
    document.getElementById("refreshDeploymentDoctorBtn").addEventListener("click", async () => {
      await app.loadDeploymentDoctor();
    });
    document.getElementById("refreshRemoteWorkerTargetsBtn").addEventListener("click", async () => {
      try {
        await app.loadRemoteWorkerTargets();
        app.setMessage(ui.serverMessage, "Remote worker targets refreshed");
      } catch (error) {
        app.setMessage(ui.serverMessage, String(error.message || error), true);
      }
    });
    ui.remoteWorkerTarget.addEventListener("change", () => {
      app.applyRemoteWorkerTarget(ui.remoteWorkerTarget.value);
    });
    document.getElementById("remoteWorkerPreflightBtn").addEventListener("click", async () => {
      try {
        await app.runRemoteWorkerPreflight();
        app.setMessage(ui.serverMessage, "Remote worker preflight completed");
      } catch (error) {
        app.setMessage(ui.serverMessage, String(error.message || error), true);
      }
    });
    document.getElementById("remoteWorkerInstallDryRunBtn").addEventListener("click", async () => {
      try {
        await app.runRemoteWorkerInstallDryRun();
        app.setMessage(ui.serverMessage, "Remote worker install dry-run completed");
      } catch (error) {
        app.setMessage(ui.serverMessage, String(error.message || error), true);
      }
    });
    document.getElementById("remoteWorkerInstallApplyBtn").addEventListener("click", async () => {
      try {
        await app.runRemoteWorkerInstallApply();
        app.setMessage(ui.serverMessage, "Remote worker install apply completed");
        await app.loadServers();
      } catch (error) {
        app.setMessage(ui.serverMessage, String(error.message || error), true);
      }
    });
    document.getElementById("remoteWorkerServiceBtn").addEventListener("click", async () => {
      try {
        await app.runRemoteWorkerServiceAction();
        app.setMessage(ui.serverMessage, "Remote worker service action completed");
        await app.loadServers();
      } catch (error) {
        app.setMessage(ui.serverMessage, String(error.message || error), true);
      }
    });
    document.getElementById("previewWorkerScaleBtn").addEventListener("click", async () => {
      try {
        await app.runWorkerScalePlan();
      } catch (error) {
        ui.workerScaleResult.innerHTML = `<span class="${app.statusClass("failed")}">${app.escapeHtml(String(error.message || error))}</span>`;
      }
    });
    document.getElementById("applyWorkerScaleBtn").addEventListener("click", async () => {
      try {
        await app.applyWorkerScale();
      } catch (error) {
        ui.workerScaleResult.innerHTML = `<span class="${app.statusClass("failed")}">${app.escapeHtml(String(error.message || error))}</span>`;
      }
    });
    document.getElementById("preflightJobBtn").addEventListener("click", async () => {
      try {
        await app.runJobPreflight();
      } catch (error) {
        app.setMessage(ui.jobMessage, String(error.message || error), true);
      }
    });
    document.getElementById("createJobBtn").addEventListener("click", async () => {
      try {
        await app.createJob();
      } catch (error) {
        app.setMessage(ui.jobMessage, String(error.message || error), true);
      }
    });
    ui.serversBody.addEventListener("click", async (event) => {
      const target = event.target;
      if (target.classList.contains("workerDetailsToggle") && target.dataset.id) {
        if (state.openWorkerDetails.has(target.dataset.id)) {
          state.openWorkerDetails.delete(target.dataset.id);
        } else {
          state.openWorkerDetails.add(target.dataset.id);
        }
        await app.loadServers();
        return;
      }
      if (target.classList.contains("scaleWorkerBtn") && target.dataset.id) {
        app.selectWorkerScaleTarget(target.dataset.id);
        return;
      }
      if (target.classList.contains("removeServerBtn") && target.dataset.id) {
        try {
          await app.archiveServer(target.dataset.id);
          app.setMessage(ui.serverMessage, `Removed server: ${target.dataset.id}`);
        } catch (error) {
          app.setMessage(ui.serverMessage, String(error.message || error), true);
        }
      }
    });
    document.getElementById("refreshJobsBtn").addEventListener("click", async () => {
      try {
        await app.loadJobs();
        app.setMessage(ui.jobsMessage, "Jobs list refreshed");
      } catch (error) {
        app.setMessage(ui.jobsMessage, String(error.message || error), true);
      }
    });
    ui.jobStatusFilter.addEventListener("change", async () => {
      state.jobsPage.status = ui.jobStatusFilter.value;
      state.jobsPage.offset = 0;
      await app.loadJobs();
    });
    ui.jobPageSize.addEventListener("change", async () => {
      state.jobsPage.limit = Number(ui.jobPageSize.value || 50);
      state.jobsPage.offset = 0;
      await app.loadJobs();
    });
    ui.includeArchivedJobs.addEventListener("change", async () => {
      state.jobsPage.includeArchived = ui.includeArchivedJobs.checked;
      state.jobsPage.offset = 0;
      await app.loadJobs();
    });
    ui.previousJobsBtn.addEventListener("click", async () => {
      state.jobsPage.offset = Math.max(0, state.jobsPage.offset - state.jobsPage.limit);
      await app.loadJobs();
    });
    ui.nextJobsBtn.addEventListener("click", async () => {
      state.jobsPage.offset += state.jobsPage.limit;
      await app.loadJobs();
    });
    ui.jobsBody.addEventListener("click", async (event) => {
      const target = event.target;
      if (target.classList.contains("jobDetailsToggle") && target.dataset.id) {
        if (state.openJobDetails.has(target.dataset.id)) {
          state.openJobDetails.delete(target.dataset.id);
        } else {
          state.openJobDetails.add(target.dataset.id);
        }
        await app.loadJobs();
        return;
      }
      if (target.classList.contains("inspectShardsBtn") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        inspector.open = true;
        inspector.offset = 0;
        await app.loadShardInspector(target.dataset.id);
        return;
      }
      if (target.classList.contains("refreshShardsBtn") && target.dataset.id) {
        await app.loadShardInspector(target.dataset.id);
        return;
      }
      if (target.classList.contains("closeShardsBtn") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        inspector.open = false;
        await app.loadJobs();
        return;
      }
      if (target.classList.contains("shardPageBtn") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        const direction = target.dataset.direction;
        const nextOffset = direction === "next"
          ? inspector.offset + inspector.limit
          : Math.max(0, inspector.offset - inspector.limit);
        inspector.offset = nextOffset;
        await app.loadShardInspector(target.dataset.id);
        return;
      }
      if (target.classList.contains("checkManifestIntegrityBtn") && target.dataset.id) {
        await app.checkManifestIntegrity(target.dataset.id);
        return;
      }
      if (target.classList.contains("requestWorkerManifestIntegrityBtn") && target.dataset.id) {
        await app.requestWorkerManifestIntegrity(target.dataset.id);
        return;
      }
      if (target.classList.contains("checkManifestFreezeBtn") && target.dataset.id) {
        await app.checkManifestFreezeReport(target.dataset.id);
        return;
      }
      if (target.classList.contains("showAttemptsBtn") && target.dataset.jobId && target.dataset.shardId) {
        await app.loadShardAttempts(target.dataset.jobId, target.dataset.shardId, 0);
        return;
      }
      if (target.classList.contains("attemptPageBtn") && target.dataset.jobId && target.dataset.shardId) {
        const limit = Number(target.dataset.limit || 100);
        const offset = Number(target.dataset.offset || 0);
        const direction = target.dataset.direction;
        const nextOffset = direction === "next"
          ? offset + limit
          : Math.max(0, offset - limit);
        await app.loadShardAttempts(target.dataset.jobId, target.dataset.shardId, nextOffset);
        return;
      }
      if (target.classList.contains("showJobLogsBtn") && target.dataset.id) {
        await app.loadJobLogs(target.dataset.id, 0);
        return;
      }
      if (target.classList.contains("logPageBtn") && target.dataset.id) {
        const limit = Number(target.dataset.limit || 100);
        const offset = Number(target.dataset.offset || 0);
        const direction = target.dataset.direction;
        const nextOffset = direction === "next"
          ? offset + limit
          : Math.max(0, offset - limit);
        await app.loadJobLogs(target.dataset.id, nextOffset);
        return;
      }
      if (target.classList.contains("showRecentErrorsBtn") && target.dataset.id) {
        await app.loadRecentErrors(target.dataset.id, 0);
        return;
      }
      if (target.classList.contains("recentErrorPageBtn") && target.dataset.id) {
        const limit = Number(target.dataset.limit || 100);
        const offset = Number(target.dataset.offset || 0);
        const direction = target.dataset.direction;
        const nextOffset = direction === "next"
          ? offset + limit
          : Math.max(0, offset - limit);
        await app.loadRecentErrors(target.dataset.id, nextOffset);
        return;
      }
      if (target.classList.contains("stopBtn") && target.dataset.id) {
        try {
          await app.stopJob(target.dataset.id);
        } catch (error) {
          app.setMessage(ui.jobsMessage, String(error.message || error), true);
        }
      }
      if (target.classList.contains("archiveJobBtn") && target.dataset.id) {
        try {
          await app.archiveJob(target.dataset.id);
          app.setMessage(ui.jobsMessage, `Archived job: ${target.dataset.id}`);
        } catch (error) {
          app.setMessage(ui.jobsMessage, String(error.message || error), true);
        }
      }
      if (target.classList.contains("deleteBtn") && target.dataset.id) {
        try {
          await app.deleteJob(target.dataset.id);
          app.setMessage(ui.jobsMessage, `Deleted job: ${target.dataset.id}`);
        } catch (error) {
          app.setMessage(ui.jobsMessage, String(error.message || error), true);
        }
      }
    });
    ui.jobsBody.addEventListener("change", async (event) => {
      const target = event.target;
      if (target.classList.contains("shardStatusSelect") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        inspector.status = target.value;
        inspector.offset = 0;
        await app.loadShardInspector(target.dataset.id);
      }
      if (target.classList.contains("shardWorkerFilter") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        inspector.workerId = target.value.trim();
        inspector.offset = 0;
        await app.loadShardInspector(target.dataset.id);
      }
      if (target.classList.contains("shardFailureCategoryFilter") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        inspector.failureCategory = target.value.trim();
        inspector.offset = 0;
        await app.loadShardInspector(target.dataset.id);
      }
      if (target.classList.contains("shardMinAttemptsFilter") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        inspector.minAttemptCount = target.value.trim();
        inspector.offset = 0;
        await app.loadShardInspector(target.dataset.id);
      }
      if (target.classList.contains("shardRunningLongerFilter") && target.dataset.id) {
        const inspector = app.shardInspectorState(target.dataset.id);
        inspector.runningLongerThanSeconds = target.value.trim();
        inspector.offset = 0;
        await app.loadShardInspector(target.dataset.id);
      }
    });
  }

  return { bindEvents };
}
