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
		var pty *bool
		if mapHasKey(pm, "pty") {
			v := asBool(pm["pty"])
			pty = &v
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
			Argv:         argv,
			Cwd:          cwd,
			Env:          env,
			TimeoutMs:    timeoutMs,
			YieldMs:      yieldMs,
			Pty:          pty,
			NotifyOnExit: notifyOnExit,
			StdinText:    stdinText,
		})
		return outcomeFromRunResult(res, "SPAWN_FAILED", "unknown error")

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

	case "process.write":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		data := asString(pm["data"])
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.write requires jobId"}}
		}
		if data == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.write requires data"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		return outcomeFromRunResult(ci.Store.Write(jobID, data), "WRITE_FAILED", "process write failed")

	case "process.send_keys":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.send_keys requires jobId"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		keys := coerceStringArray(pm["keys"])
		if mapHasKey(pm, "keys") && keys == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.send_keys keys must be string[]"}}
		}
		literal := asString(pm["literal"])
		data, warnings := jobstore.EncodeKeySequence(keys, literal)
		if data == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.send_keys requires keys or literal"}}
		}
		outcome := outcomeFromRunResult(ci.Store.Write(jobID, data), "WRITE_FAILED", "process send_keys failed")
		if outcome.OK && len(warnings) > 0 {
			if payload, ok := outcome.Payload.(map[string]interface{}); ok {
				payload["warnings"] = warnings
				outcome.Payload = payload
			}
		}
		return outcome

	case "process.submit":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.submit requires jobId"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		return outcomeFromRunResult(ci.Store.Write(jobID, "\r"), "WRITE_FAILED", "process submit failed")

	case "process.paste":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		text := asString(pm["text"])
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.paste requires jobId"}}
		}
		if text == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.paste requires text"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		bracketed := false
		if mapHasKey(pm, "bracketed") {
			bracketed = asBool(pm["bracketed"])
		}
		data := jobstore.EncodePaste(text, bracketed)
		return outcomeFromRunResult(ci.Store.Write(jobID, data), "WRITE_FAILED", "process paste failed")

	case "process.kill":
		pm, _ := req.Params.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		if jobID == "" {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "BAD_INPUT", Message: "process.kill requires jobId"}}
		}
		if ci.Store == nil {
			return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "NOT_READY", Message: "node store not initialized"}}
		}
		return outcomeFromRunResult(ci.Store.Kill(jobID), "NOT_RUNNING", "job not running")

	default:
		return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "UNSUPPORTED", Message: "unsupported command: " + cmd}}
	}
}

func outcomeFromRunResult(res jobstore.RunResult, defaultCode string, defaultMessage string) InvokeOutcome {
	if res.OK {
		return InvokeOutcome{OK: true, Payload: res.Payload}
	}
	if res.Error != nil {
		return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: res.Error.Code, Message: res.Error.Message}}
	}
	return InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: defaultCode, Message: defaultMessage}}
}
