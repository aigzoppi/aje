import { useMemo, useState } from "react";
import {
  Alert, AppBar, Box, Button, Chip, Container, IconButton, Paper, Snackbar, Stack, Toolbar, Tooltip, Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import RefreshIcon from "@mui/icons-material/Refresh";
import { api, TERMINAL } from "./api.js";
import { useJobsFeed } from "./hooks/useJobsFeed.js";
import JobsGrid from "./components/JobsGrid.jsx";
import SubmitJobDialog from "./components/SubmitJobDialog.jsx";
import JobDetailDrawer from "./components/JobDetailDrawer.jsx";

const CONNECTION = {
  connecting: { label: "connecting", color: "default" },
  live: { label: "live", color: "success" },
  polling: { label: "stream down · polling", color: "warning" },
};

export default function App() {
  const { jobs, connection, error, refresh, upsert } = useJobsFeed();
  const [submitOpen, setSubmitOpen] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const [toast, setToast] = useState(null);

  const selected = useMemo(() => jobs.find((j) => j.id === selectedId) || null, [jobs, selectedId]);
  const counts = useMemo(() => {
    const live = jobs.filter((j) => !TERMINAL.has(j.status)).length;
    const failed = jobs.filter((j) => j.status === "failed").length;
    return { total: jobs.length, live, failed };
  }, [jobs]);

  const notify = (severity, message) => setToast({ severity, message });

  const submit = async (spec) => {
    const job = await api.submitJob(spec);
    upsert([job]);
    notify("success", `Submitted ${job.name}`);
    setSelectedId(job.id);
  };

  // All row / drawer commands go through here; the returned Job is merged into the grid.
  const onAction = async (action, job, arg) => {
    try {
      let result;
      switch (action) {
        case "cancel":
          result = await api.cancelJob(job.id);
          break;
        case "suspend":
          result = await api.suspend(job.id);
          break;
        case "resume":
          result = await api.resume(job.id);
          break;
        case "close":
          result = await api.close(job.id);
          break;
        case "scale":
          result = await api.scale(job.id, arg);
          break;
        case "management":
          result = await api.setManagement(job.id, arg);
          break;
        case "addTasks": {
          const r = await api.addTasks(job.id, arg.items, arg.close);
          notify("success", `Added ${r.task_ids.length} tasks`);
          return;
        }
        case "addBatch": {
          const r = await api.addBatch(job.id, arg.items);
          notify("success", `Batch ${r.batch_id} in ${r.partitions} partitions`);
          return;
        }
        case "error":
          notify("error", arg);
          return;
        default:
          return;
      }
      if (result?.id) upsert([result]);
      notify("success", `${action}: ${job.name}`);
    } catch (e) {
      notify("error", `${action} failed: ${e.message}`);
    }
  };

  const conn = CONNECTION[connection];

  return (
    <Box sx={{ minHeight: "100vh", bgcolor: "background.default" }}>
      <AppBar position="static" elevation={0}>
        <Toolbar>
          <Typography variant="h6" sx={{ flex: 1 }}>
            AJE Jobs Console
          </Typography>
          <Chip size="small" label={conn.label} color={conn.color} sx={{ mr: 2, bgcolor: connection === "connecting" ? "grey.300" : undefined }} />
          <Tooltip title="Reload jobs">
            <IconButton color="inherit" onClick={refresh}>
              <RefreshIcon />
            </IconButton>
          </Tooltip>
          <Button color="inherit" variant="outlined" startIcon={<AddIcon />} onClick={() => setSubmitOpen(true)} sx={{ ml: 1 }}>
            Submit job
          </Button>
        </Toolbar>
      </AppBar>

      <Container maxWidth="xl" sx={{ py: 3 }}>
        <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
          <Chip label={`${counts.total} jobs`} />
          <Chip label={`${counts.live} live`} color="primary" variant="outlined" />
          <Chip label={`${counts.failed} failed`} color={counts.failed ? "error" : "default"} variant="outlined" />
        </Stack>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            Could not load jobs: {error}
          </Alert>
        )}
        <Paper sx={{ height: "calc(100vh - 190px)", minHeight: 400 }}>
          <JobsGrid jobs={jobs} loading={connection === "connecting" && jobs.length === 0} onOpen={setSelectedId} onAction={onAction} />
        </Paper>
      </Container>

      <SubmitJobDialog open={submitOpen} onClose={() => setSubmitOpen(false)} onSubmit={submit} />
      <JobDetailDrawer job={selected} onClose={() => setSelectedId(null)} onAction={onAction} />

      <Snackbar
        open={Boolean(toast)}
        autoHideDuration={4000}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
      >
        {toast ? (
          <Alert severity={toast.severity} onClose={() => setToast(null)} variant="filled">
            {toast.message}
          </Alert>
        ) : undefined}
      </Snackbar>
    </Box>
  );
}
