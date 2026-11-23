import { useMemo, useState } from "react";
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, Grid, MenuItem,
  Switch, Tab, Tabs, TextField,
} from "@mui/material";

const DNS_LABEL = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

const PRESETS = {
  farm: { handler: "aje_worker.examples:square", input: "[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]" },
  data_parallel: { handler: "aje_worker.examples:sum_squares", input: "[1, 2, 3, 4, 5, 6, 7, 8]" },
};

const initialForm = (pattern = "farm") => ({
  name: "",
  pattern,
  command: `python -m aje_worker ${PRESETS[pattern].handler}`,
  image: "",
  min_workers: 1,
  max_workers: 8,
  management: "active",
  max_retries: 3,
  output_queue: "",
  input: PRESETS[pattern].input,
  close_when_done: true,
  gather: "sum",
  partitions: 4,
});

function parseInput(text) {
  if (!text.trim()) return { value: undefined };
  try {
    const value = JSON.parse(text);
    return Array.isArray(value) ? { value } : { error: "must be a JSON array" };
  } catch (e) {
    return { error: e.message };
  }
}

// Builds the POST /v1/jobs body from the form (see README "Job request").
function buildSpec(f) {
  const spec = {
    name: f.name,
    pattern: f.pattern,
    worker: { command: f.command.trim().split(/\s+/).filter(Boolean) },
    min_workers: Number(f.min_workers),
    max_workers: Number(f.max_workers),
    management: f.management,
    max_retries: Number(f.max_retries),
  };
  if (f.image.trim()) spec.worker.image = f.image.trim();
  if (f.output_queue.trim()) spec.output_queue = f.output_queue.trim();
  const input = parseInput(f.input).value;
  if (f.pattern === "farm") {
    if (input) spec.tasks = input;
    spec.close_when_done = f.close_when_done;
  } else {
    spec.gather = f.gather;
    spec.partitions = Number(f.partitions);
    if (input) spec.items = input;
  }
  return spec;
}

export default function SubmitJobDialog({ open, onClose, onSubmit }) {
  const [form, setForm] = useState(initialForm());
  const [tab, setTab] = useState(0); // 0 = form, 1 = raw JSON
  const [raw, setRaw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const set = (key) => (e) =>
    setForm((f) => ({ ...f, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  const errors = useMemo(() => {
    const e = {};
    if (!DNS_LABEL.test(form.name)) e.name = "lowercase letters, digits and '-' (DNS-1123 label)";
    if (!form.command.trim()) e.command = "required";
    if (Number(form.min_workers) < 0) e.min_workers = "must be >= 0";
    if (Number(form.max_workers) < Number(form.min_workers)) e.max_workers = "must be >= min workers";
    const input = parseInput(form.input);
    if (input.error) e.input = input.error;
    return e;
  }, [form]);

  const changePattern = (e) => {
    const pattern = e.target.value;
    setForm((f) => ({
      ...f,
      pattern,
      command: `python -m aje_worker ${PRESETS[pattern].handler}`,
      input: PRESETS[pattern].input,
    }));
  };

  const switchTab = (_e, t) => {
    if (t === 1) setRaw(JSON.stringify(buildSpec(form), null, 2));
    setTab(t);
    setError(null);
  };

  const submit = async () => {
    setError(null);
    let spec;
    try {
      spec = tab === 1 ? JSON.parse(raw) : buildSpec(form);
    } catch (e) {
      setError(`Invalid JSON: ${e.message}`);
      return;
    }
    setBusy(true);
    try {
      await onSubmit(spec);
      setForm(initialForm(form.pattern));
      setTab(0);
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = tab === 1 ? Boolean(raw.trim()) : Object.keys(errors).length === 0;
  const field = (key, props = {}) => ({
    value: form[key],
    onChange: set(key),
    error: Boolean(errors[key]) && form[key] !== "",
    fullWidth: true,
    size: "small",
    ...props,
    helperText: (form[key] !== "" && errors[key]) || props.helperText,
  });

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle>Submit a job</DialogTitle>
      <Tabs value={tab} onChange={switchTab} sx={{ px: 3 }}>
        <Tab label="Form" />
        <Tab label="JSON" />
      </Tabs>
      <DialogContent dividers>
        {tab === 0 ? (
          <Grid container spacing={2}>
            <Grid item xs={12} sm={8}>
              <TextField label="Name" autoFocus required {...field("name")} />
            </Grid>
            <Grid item xs={12} sm={4}>
              <TextField select label="Pattern" {...field("pattern")} onChange={changePattern}>
                <MenuItem value="farm">farm</MenuItem>
                <MenuItem value="data_parallel">data_parallel</MenuItem>
              </TextField>
            </Grid>
            <Grid item xs={12} sm={8}>
              <TextField label="Worker command" required {...field("command", { helperText: "space-separated argv" })} />
            </Grid>
            <Grid item xs={12} sm={4}>
              <TextField label="Image (Kubernetes only)" {...field("image")} />
            </Grid>
            <Grid item xs={6} sm={3}>
              <TextField type="number" label="Min workers" {...field("min_workers")} />
            </Grid>
            <Grid item xs={6} sm={3}>
              <TextField type="number" label="Max workers" {...field("max_workers")} />
            </Grid>
            <Grid item xs={6} sm={3}>
              <TextField type="number" label="Max retries" {...field("max_retries")} />
            </Grid>
            <Grid item xs={6} sm={3}>
              <TextField select label="Management" {...field("management")}>
                <MenuItem value="active">active</MenuItem>
                <MenuItem value="passive">passive</MenuItem>
              </TextField>
            </Grid>
            {form.pattern === "data_parallel" && (
              <>
                <Grid item xs={6}>
                  <TextField select label="Gather" {...field("gather")}>
                    {["list", "concat", "sum", "merge"].map((g) => (
                      <MenuItem key={g} value={g}>
                        {g}
                      </MenuItem>
                    ))}
                  </TextField>
                </Grid>
                <Grid item xs={6}>
                  <TextField type="number" label="Partitions" {...field("partitions")} />
                </Grid>
              </>
            )}
            <Grid item xs={12}>
              <TextField
                label={form.pattern === "farm" ? "Tasks (JSON array, optional)" : "First batch items (JSON array, optional)"}
                multiline
                minRows={3}
                {...field("input")}
                InputProps={{ sx: { fontFamily: "monospace" } }}
              />
            </Grid>
            <Grid item xs={12} sm={8}>
              <TextField label="Output queue (optional)" {...field("output_queue")} />
            </Grid>
            {form.pattern === "farm" && (
              <Grid item xs={12} sm={4}>
                <FormControlLabel
                  control={<Switch checked={form.close_when_done} onChange={set("close_when_done")} />}
                  label="Close when done"
                />
              </Grid>
            )}
          </Grid>
        ) : (
          <TextField
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            multiline
            minRows={18}
            fullWidth
            InputProps={{ sx: { fontFamily: "monospace", fontSize: 13 } }}
            helperText="Full job request body. Any field from the README's Job request section is accepted."
          />
        )}
        {error && (
          <Box mt={2}>
            <Alert severity="error">{error}</Alert>
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button variant="contained" onClick={submit} disabled={!canSubmit || busy}>
          {busy ? "Submitting…" : "Submit"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
