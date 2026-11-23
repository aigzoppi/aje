import React from "react";
import ReactDOM from "react-dom/client";
import { createTheme, CssBaseline, ThemeProvider, useMediaQuery } from "@mui/material";
import App from "./App.jsx";

function Root() {
  const dark = useMediaQuery("(prefers-color-scheme: dark)");
  const theme = React.useMemo(() => createTheme({ palette: { mode: dark ? "dark" : "light" } }), [dark]);
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <App />
    </ThemeProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
