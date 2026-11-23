import { Box, Chip, LinearProgress, Typography } from "@mui/material";

const STATUS_COLOR = {
  pending: "default",
  running: "primary",
  suspended: "warning",
  draining: "info",
  succeeded: "success",
  failed: "error",
  cancelled: "default",
};

export function StatusChip({ status }) {
  return (
    <Chip
      size="small"
      label={status}
      color={STATUS_COLOR[status] || "default"}
      variant={status === "cancelled" ? "outlined" : "filled"}
    />
  );
}

// farm: done = completed + failed of submitted; a percentage only once the input is closed.
// data_parallel: batches done/failed of all batches seen.
export function progressOf(job) {
  const c = job.counts || {};
  if (job.pattern === "data_parallel") {
    const done = (c.batches_done || 0) + (c.batches_failed || 0);
    const total = done + (c.batches_pending || 0);
    return { done, total, unit: "batches", final: job.closed };
  }
  return { done: (c.completed || 0) + (c.failed || 0), total: c.submitted || 0, unit: "tasks", final: job.closed };
}

export function JobProgress({ job }) {
  const { done, total, unit, final } = progressOf(job);
  const pct = total ? (100 * done) / total : 0;
  const failed = job.pattern === "data_parallel" ? job.counts?.batches_failed : job.counts?.failed;
  return (
    <Box sx={{ width: "100%" }}>
      <LinearProgress
        variant={final || job.status === "succeeded" ? "determinate" : "indeterminate"}
        value={pct}
        color={failed ? "warning" : "primary"}
        sx={{ height: 6, borderRadius: 3, opacity: ["running", "draining"].includes(job.status) || final ? 1 : 0.4 }}
      />
      <Typography variant="caption" color="text.secondary">
        {done}/{total} {unit}
        {failed ? ` · ${failed} failed` : ""}
      </Typography>
    </Box>
  );
}

export const fmtNum = (v, digits = 1) => (v === null || v === undefined ? "–" : Number(v).toFixed(digits));
export const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "–");
