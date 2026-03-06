package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/jobstore"
)

func main() {
	stateDir, err := os.MkdirTemp("", "argus-jobstore-smoke-")
	if err != nil {
		panic(err)
	}
	defer os.RemoveAll(stateDir)

	store, err := jobstore.New(filepath.Join(stateDir, "state"), "smoke-node", nil)
	if err != nil {
		panic(err)
	}

	usePty := true
	yieldMs := 0
	res := store.Run(jobstore.RunParams{
		Argv:    []string{"bash", "-lc", "read -r answer; echo answer=$answer"},
		Pty:     &usePty,
		YieldMs: &yieldMs,
	})
	if !res.OK {
		panic(fmt.Sprintf("run failed: %#v", res.Error))
	}

	payloadJSON, _ := json.Marshal(res.Payload)
	var runPayload struct {
		JobID string `json:"jobId"`
	}
	if err := json.Unmarshal(payloadJSON, &runPayload); err != nil {
		panic(err)
	}
	if strings.TrimSpace(runPayload.JobID) == "" {
		panic("missing jobId")
	}

	if writeRes := store.Write(runPayload.JobID, "agree"); !writeRes.OK {
		panic(fmt.Sprintf("write failed: %#v", writeRes.Error))
	}
	if submitRes := store.Write(runPayload.JobID, "\r"); !submitRes.OK {
		panic(fmt.Sprintf("submit failed: %#v", submitRes.Error))
	}

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		job := store.GetJob(runPayload.JobID)
		if job != nil && !job.Running {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	job := store.GetJob(runPayload.JobID)
	if job == nil || job.Running {
		panic("job still running")
	}
	logs := store.GetLogs(runPayload.JobID, 8192)
	if logs == nil {
		panic("missing logs")
	}
	if !strings.Contains(logs.Stdout, "answer=agree") {
		panic(fmt.Sprintf("unexpected stdout: %q", logs.Stdout))
	}
	fmt.Println("smoke ok", runPayload.JobID)
}
