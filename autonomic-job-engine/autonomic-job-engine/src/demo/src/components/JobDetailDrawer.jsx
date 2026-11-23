import { useEffect, useRef, useState } from "react";
import {
  Alert, Box, Button, Chip, Divider, Drawer, FormControlLabel, IconButton, Stack, Switch, TextField, Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import { api, TERMINAL } from "../api.js";
import { useJobStream } from "../hooks/useJobStream.js";
import { fmtNum, fmtTime, JobProgress, StatusChip } from "./format.jsx";

const EVENT_COLOR = (kind) => {
  if (/failed|error/.test(kind)) return "error";
  if (/succeeded|done/.test(kind)) return "success";
  if (/cancelled|suspended/.test(kind)) return "warning";
  if (/scaled|repartitioned/.test(kind)) return "info";
  return "default";
};

function Metric({ label, value }) {
  return (
    <Box sx={{ minWidth: 96 }}>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Typography variant="body2" fontWeight={600}>
        {value}
      </Typography>
    </Box>
  );
}

// Feed more input to a live job: tasks (farm) or a batch (data_parallel).
function FeedPanel({ job, onAction }) {
  const [text, setText] = useState("[1, 2, 3]");
  const [close, setClose] = useState(false);
  const farm = job.pattern === "farm";
  const send = () => {
    let items;
    try {
      items = JSON.parse(text);
      if (!Array.isArray(items)) throw new Error("must be a JSON array");
    } catch (e) {
      onAction("error", job, e.message);
      return;
    }
    if (farm) onAction("addTasks", job, { items, close });
    else onAction("addBatch", job, { items });
  };
  return (
    <Stack spacing={1}>
      <Typography variant="subtitle2">{farm ? "Add tasks" : "Add a batch"}</Typography>
      <TextField
        size="small"
        multiline
        minRows={2}
        value={text}
        onChange={(e) => setText(e.target.value)}
        InputProps={{ sx: { fontFamily: "monospace" } }}
      />
      <Stack direction="row" spacing={1} alignItems="center">
        <Button size="small" variant="outlined" onClick={send}>
          Send
        </Button>
        {farm && (
          <FormControlLabel
            control={<Switch size="small" checked={close} onChange={(e) => setClose(e.target.checked)} />}
            label="and close"
          />
        )}
        <Box flex={1} />
        <Button size="small" onClick={() => onAction("close", job)}>
          Close input
        </Button>
      </Stack>
    </Stack>
  );
}

export default function JobDetailDrawer({ job, onClose, onAction }) {
  const { events, metrics, ended } = useJobStream(job?.id);
  const [workers, setWorkers] = useState("");
  const logEnd = useRef(null);

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: "end" });
  }, [events.length]);

  useEffect(() => setWorkers(""), [job?.id]);

  if (!job) return null;
  const live = !TERMINAL.has(job.status) && !ended;
  const m = { ...(job.metrics || {}), ...(metrics || {}) };

  return (
    <Drawer anchor="right" open={Boolean(job)} onClose={onClose} PaperProps={{ sx: { width: { xs: "100%", sm: 560 } } }}>
      <Stack sx={{ p: 2, height: "100%" }} spacing={2}>
        <Stack direction="row" alignItems="center" spacing={1}>
          <Typography variant="h6" sx={{ flex: 1 }} noWrap>
            {job.name}
          </Typography>
          <StatusChip status={metrics?.status || job.status} />
          <IconButton onClick={onClose} size="small">
            <CloseIcon />
          </IconButton>
        </Stack>
        <Typography variant="caption" color="text.secondary">
          <code>{job.id}</code> · {job.pattern} · created {fmtTime(job.created_at)}
          {job.finished_at && ` · finished ${fmtTime(job.finished_at)}`}
        </Typography>
        {job.message && <Alert severity={job.status === "failed" ? "error" : "info"}>{job.message}</Alert>}

        <JobProgress job={job} />

        <Stack direction="row" flexWrap="wrap" useFlexGap spacing={2}>
          <Metric label="Workers" value={`${metrics?.workers ?? job.workers ?? 0} (${job.min_workers}–${job.max_workers})`} />
          <Metric label="Throughput /min" value={fmtNum(m.throughput)} />
          <Metric label="Queue depth" value={fmtNum(m.queue_depth, 0)} />
          <Metric label="In flight" value={fmtNum(m.in_flight, 0)} />
          <Metric label="Avg latency s" value={fmtNum(m.avg_latency, 2)} />
          <Metric label="p95 latency s" value={fmtNum(m.p95_latency, 2)} />
          <Metric label="Utilisation" value={m.utilisation == null ? "–" : `${Math.round(m.utilisation * 100)}%`} />
          <Metric label="Retried" value={job.counts?.retried ?? 0} />
        </Stack>

        {live && (
          <>
            <Divider />
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <TextField
                size="small"
                type="number"
                label="Workers"
                value={workers}
                onChange={(e) => setWorkers(e.target.value)}
                sx={{ width: 110 }}
              />
              <Button size="small" variant="outlined" disabled={workers === ""} onClick={() => onAction("scale", job, Number(workers))}>
                Scale
              </Button>
              <Button
                size="small"
                onClick={() => onAction("management", job, job.management === "active" ? "passive" : "active")}
              >
                Management: {job.management}
              </Button>
              {job.status === "suspended" ? (
                <Button size="small" onClick={() => onAction("resume", job)}>
                  Resume
                </Button>
              ) : (
                <Button size="small" onClick={() => onAction("suspend", job)}>
                  Suspend
                </Button>
              )}
              <Button size="small" color="error" onClick={() => onAction("cancel", job)}>
                Cancel
              </Button>
            </Stack>
            {!job.closed && <FeedPanel job={job} onAction={onAction} />}
          </>
        )}

        <Divider />
        <Typography variant="subtitle2">
          Events {ended ? `(stream ended: ${ended})` : live ? "(live)" : ""}
        </Typography>
        <Box sx={{ flex: 1, overflow: "auto", fontSize: 13, minHeight: 120 }}>
          {events.map((ev) => (
            <Stack key={ev.id ?? `${ev.ts}-${ev.kind}`} direction="row" spacing={1} alignItems="baseline" sx={{ py: 0.5 }}>
              <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
                {new Date(ev.ts).toLocaleTimeString()}
              </Typography>
              <Chip size="small" label={ev.kind} color={EVENT_COLOR(ev.kind)} variant="outlined" />
              <Typography variant="body2">{ev.message}</Typography>
            </Stack>
          ))}
          {events.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              Waiting for events…
            </Typography>
          )}
          <div ref={logEnd} />
        </Box>
      </Stack>
    </Drawer>
  );
}
