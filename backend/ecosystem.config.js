module.exports = {
  apps: [
    {
      name: "thinksync",
      cwd: "/root/thinksync/backend",
      script: "uvicorn",
      args: "main:app --host 0.0.0.0 --port 8000",
      interpreter: "none",
      env: {
        NODE_ENV: "production"
      }
    }
  ]
}
