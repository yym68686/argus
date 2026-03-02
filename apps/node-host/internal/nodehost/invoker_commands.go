package nodehost

import (
	"context"
	"os/exec"
	"strings"

	"github.com/yym68686/argus/apps/node-host/internal/jobstore"
	"github.com/yym68686/argus/apps/node-host/internal/proto"
)

type CommandInvoker struct {
	Store *jobstore.Store
}

func (ci *CommandInvoker) Invoke(ctx context.Context, req InvokeRequest) InvokeOutcome {
	_ = ctx
	cmd := strings.TrimSpace(req.Command)

	switch cmd {
	case "system.run":
		pm, _ := req.Params.(map[string]interface{})
		argv := coerceStringArray(pm["argv"])
		cwdRaw := strings.TrimSpace(asString(pm["cwd"]))
		var cwd *string
		if cwdRaw != "" {
			cwd = &cwdRaw
		}
		var timeoutMs *int
		if n, ok := asNumber(pm["timeoutMs"]); ok {
			v := int(n)
			timeoutMs = &v
		}
		var yieldMs *int
		if n, ok := asNumber(pm["yieldMs"]); ok {
			v := int(n)
			yieldMs = &v
		}
		var stdinText *string
		if s := asString(pm["stdinText"]); s != "" {
			stdinText = &s
		}
		var notifyOnExit *bool
		if mapHasKey(pm, "notifyOnExit") {
			v := asBool(pm["notifyOnExit"])
			notifyOnExit = &v
		}
		env := jobstore.EnvMap(pm["env"])

		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		res := ci.Store.Run(jobstore.RunParams{
			Argv:        argv,
			Cwd:         cwd,
			Env:         env,
			TimeoutMs:   timeoutMs,
			YieldMs:     yieldMs,
			NotifyOnExit: notifyOnExit,
			StdinText:   stdinText,
		})
		if res.OK {
			return InvokeOutcome{OK: true, Payload: res.Payload}
		}
		if res.Error != nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: res.Error.Code, Message: res.Error.Message}}
		}
		return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "SPAWN_FAILED", Message: "unknown error"}}

	case "system.which":
		pm, _ := req.Params.(map[string]interface{})
		bin := strings.TrimSpace(asString(pm["bin"]))
		if bin == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "system.which requires bin"}}
		}
		path, err := exec.LookPath(bin)
		if err != nil {
			return InvokeOutcome{OK: true, Payload: map[string]interface{}{"bin": bin, "path": nil}}
		}
		return InvokeOutcome{OK: true, Payload: map[string]interface{}{"bin": bin, "path": path}}

	case "process.list":
		if ci.Store == nil {
			return InvokeOutcome{OK: true, Payload: map[string]interface{}{"jobs": []interface{}{}}}
		}
		return InvokeOutcome{OK: true, Payload: map[string]interface{}{"jobs": ci.Store.ListJobs()}}

	case "process.get":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.get requires jobId"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		meta := ci.Store.GetJob(jobID)
		if meta == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_FOUND", Message: "unknown jobId: " + jobID}}
		}
		return InvokeOutcome{OK: true, Payload: map[string]interface{}{"job": meta}}

	case "process.logs":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.logs requires jobId"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		tailBytes := 0
		if n, ok := asNumber(pm["tailBytes"]); ok {
			tailBytes = int(n)
		}
		logs := ci.Store.GetLogs(jobID, tailBytes)
		if logs == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_FOUND", Message: "unknown jobId: " + jobID}}
		}
		return InvokeOutcome{OK: true, Payload: logs}

	case "process.kill":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.kill requires jobId"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		res := ci.Store.Kill(jobID)
		if res.OK {
			return InvokeOutcome{OK: true, Payload: res.Payload}
		}
		if res.Error != nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: res.Error.Code, Message: res.Error.Message}}
		}
		return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_RUNNING", Message: "job not running"}}

	default:
		return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "UNSUPPORTED", Message: "unsupported command: " + cmd}}
	}
}
