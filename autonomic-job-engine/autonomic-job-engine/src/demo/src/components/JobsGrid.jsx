import { DataGrid, GridActionsCellItem, GridToolbar } from "@mui/x-data-grid";
import CancelIcon from "@mui/icons-material/Cancel";
import PauseIcon from "@mui/icons-material/Pause";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import VisibilityIcon from "@mui/icons-material/Visibility";
import { TERMINAL } from "../api.js";
import { fmtNum, fmtTime, JobProgress, progressOf, StatusChip } from "./format.jsx";

export default function JobsGrid({ jobs, onOpen, onAction, loading }) {
  const columns = [
    { field: "name", headerName: "Name", flex: 1, minWidth: 140 },
    {
      field: "id",
      headerName: "ID",
      width: 110,
      renderCell: ({ value }) => <code title={value}>{String(value).slice(0, 8)}</code>,
    },
    { field: "pattern", headerName: "Pattern", width: 120 },
    {
      field: "status",
      headerName: "Status",
      width: 120,
      renderCell: ({ value }) => <StatusChip status={value} />,
    },
    {
      field: "progress",
      headerName: "Progress",
      width: 190,
      sortable: true,
      valueGetter: (_v, row) => {
        const p = progressOf(row);
        return p.total ? p.done / p.total : 0;
      },
      renderCell: ({ row }) => <JobProgress job={row} />,
    },
    {
      field: "workers",
      headerName: "Workers",
      width: 100,
      valueGetter: (v, row) => (row.max_workers ? `${v ?? 0} / ${row.max_workers}` : v ?? 0),
    },
    {
      field: "throughput",
      headerName: "Tasks/min",
      type: "number",
      width: 100,
      valueGetter: (_v, row) => row.metrics?.throughput ?? null,
      valueFormatter: (v) => fmtNum(v),
    },
    {
      field: "queue_depth",
      headerName: "Queue",
      type: "number",
      width: 80,
      valueGetter: (_v, row) => row.metrics?.queue_depth ?? null,
      valueFormatter: (v) => fmtNum(v, 0),
    },
    { field: "management", headerName: "Mgmt", width: 90 },
    {
      field: "created_at",
      headerName: "Created",
      width: 170,
      type: "dateTime",
      valueGetter: (v) => (v ? new Date(v) : null),
      valueFormatter: (v) => (v ? fmtTime(v) : "–"),
    },
    {
      field: "actions",
      type: "actions",
      width: 120,
      getActions: ({ row }) => {
        const live = !TERMINAL.has(row.status);
        const items = [
          <GridActionsCellItem key="open" icon={<VisibilityIcon />} label="Details" onClick={() => onOpen(row.id)} />,
        ];
        if (row.status === "running")
          items.push(
            <GridActionsCellItem key="sus" icon={<PauseIcon />} label="Suspend" onClick={() => onAction("suspend", row)} />,
          );
        if (row.status === "suspended")
          items.push(
            <GridActionsCellItem key="res" icon={<PlayArrowIcon />} label="Resume" onClick={() => onAction("resume", row)} />,
          );
        if (live)
          items.push(
            <GridActionsCellItem key="cancel" icon={<CancelIcon />} label="Cancel" onClick={() => onAction("cancel", row)} />,
          );
        return items;
      },
    },
  ];

  return (
    <DataGrid
      rows={jobs}
      columns={columns}
      loading={loading}
      density="compact"
      rowHeight={52}
      disableRowSelectionOnClick
      onRowDoubleClick={({ id }) => onOpen(id)}
      initialState={{
        sorting: { sortModel: [{ field: "created_at", sort: "desc" }] },
        pagination: { paginationModel: { pageSize: 25 } },
        columns: { columnVisibilityModel: { management: false } },
      }}
      pageSizeOptions={[10, 25, 50, 100]}
      slots={{ toolbar: GridToolbar }}
      slotProps={{ toolbar: { showQuickFilter: true } }}
    />
  );
}
