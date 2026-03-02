package jobstore

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/util"
)

const (
	defaultYieldMs       = 10_000
	defaultLogTailBytes  = 2 * 1024 * 1024
	defaultLogsTailBytes = 64 * 1024
	maxCompletedJobs     = 200
	jobDirname           = "jobs"
	maxStdinTextBytes    = 1 * 1024 * 1024
)

type InvokeError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type JobMeta struct {
	Version int `json:"version"`

	JobID string `json:"jobId"`
	NodeID string `json:"nodeId"`

	Argv []string `json:"argv"`
	Cwd  *string  `json:"cwd"`

	StdinTextBytes *int `json:"stdinTextBytes"`

	CreatedAtMs int64  `json:"createdAtMs"`
	StartedAtMs *int64 `json:"startedAtMs"`
	EndedAtMs   *int64 `json:"endedAtMs"`

	PID *int `json:"pid"`

	Running  bool `json:"running"`
	Orphaned bool `json:"orphaned"`

	TimeoutMs *int `json:"timeoutMs"`
	YieldMs   *int `json:"yieldMs"`

	NotifyOnExit     bool   `json:"notifyOnExit"`
	NotifyOnExitMode string `json:"notifyOnExitMode"`

	ExitCode *int    `json:"exitCode"`
	Signal   *string `json:"signal"`
	TimedOut bool    `json:"timedOut"`

	StdoutBytes     int64 `json:"stdoutBytes"`
	StderrBytes     int64 `json:"stderrBytes"`
	StdoutTruncated bool  `json:"stdoutTruncated"`
	StderrTruncated bool  `json:"stderrTruncated"`
}

type listJob struct {
	JobID     string   `json:"jobId"`
	Running   bool     `json:"running"`
	Orphaned  bool     `json:"orphaned"`
	PID       *int     `json:"pid"`
	Argv      []string `json:"argv"`
	Cwd       *string  `json:"cwd"`
	CreatedAt *int64   `json:"createdAtMs"`
	StartedAt *int64   `json:"startedAtMs"`
	EndedAt   *int64   `json:"endedAtMs"`
	ExitCode  *int     `json:"exitCode"`
	Signal    *string  `json:"signal"`
	TimedOut  bool     `json:"timedOut"`
}

type logsPayload struct {
	JobID string `json:"jobId"`

	Running bool `json:"running"`

	Stdout string `json:"stdout"`
	Stderr string `json:"stderr"`

	StdoutBytes int64 `json:"stdoutBytes"`
	StderrBytes int64 `json:"stderrBytes"`

	StdoutTruncated bool `json:"stdoutTruncated"`
	StderrTruncated bool `json:"stderrTruncated"`
}

type runningPayload struct {
	JobID   string   `json:"jobId"`
	Running bool     `json:"running"`
	Argv    []string `json:"argv"`
	Cwd     *string  `json:"cwd"`

	TimeoutMs *int `json:"timeoutMs"`
	YieldMs   *int `json:"yieldMs"`
	PID       *int `json:"pid"`

	StdoutTail string `json:"stdoutTail"`
	StderrTail string `json:"stderrTail"`

	StdoutBytes int64 `json:"stdoutBytes"`
	StderrBytes int64 `json:"stderrBytes"`
}

type runFinishedPayload struct {
	JobID   string   `json:"jobId"`
	Running bool     `json:"running"`
	Argv    []string `json:"argv"`
	Cwd     *string  `json:"cwd"`

	TimeoutMs *int `json:"timeoutMs"`
	YieldMs   int  `json:"yieldMs"`
	PID       *int `json:"pid"`

	ExitCode *int    `json:"exitCode"`
	Signal   *string `json:"signal"`
	TimedOut bool    `json:"timedOut"`

	Stdout string `json:"stdout"`
	Stderr string `json:"stderr"`

	StdoutBytes int64 `json:"stdoutBytes"`
	StderrBytes int64 `json:"stderrBytes"`

	StdoutTruncated bool `json:"stdoutTruncated"`
	StderrTruncated bool `json:"stderrTruncated"`
}

type JobRuntime struct {
	mu sync.Mutex

	cmd *exec.Cmd

	running bool

	stdoutTail  *TailBuffer
	stderrTail  *TailBuffer
	stdoutBytes int64
	stderrBytes int64

	timeoutTimer *time.Timer
	timedOut     bool

	doneCh chan struct{}
}

type jobRecord struct {
	meta    *JobMeta
	runtime *JobRuntime
}

type Store struct {
	stateDir string
	jobsDir  string
	nodeID   string

	sendEvent func(event string, payload interface{})

	mu   sync.Mutex
	jobs map[string]*jobRecord
}

func New(stateDir string, nodeID string, sendEvent func(event string, payload interface{})) (*Store, error) {
	if strings.TrimSpace(stateDir) == "" {
		return nil, errors.New("stateDir required")
	}
	jobsDir := filepath.Join(stateDir, jobDirname)
	if err := os.MkdirAll(jobsDir, 0o755); err != nil {
		return nil, err
	}
	s := &Store{
		stateDir:   stateDir,
		jobsDir:    jobsDir,
		nodeID:     nodeID,
		sendEvent:  sendEvent,
		jobs:       make(map[string]*jobRecord),
	}
	s.loadFromDisk()
	s.cleanupCompleted()
	return s, nil
}

func (s *Store) jobPaths(jobID string) (dir string, meta string, stdout string, stderr string) {
	dir = filepath.Join(s.jobsDir, jobID)
	return dir, filepath.Join(dir, "meta.json"), filepath.Join(dir, "stdout.log"), filepath.Join(dir, "stderr.log")
}

func (s *Store) loadFromDisk() {
	entries, err := os.ReadDir(s.jobsDir)
	if err != nil {
		return
	}
	for _, ent := range entries {
		if !ent.IsDir() {
			continue
		}
		jobID := ent.Name()
		_, metaPath, _, _ := s.jobPaths(jobID)
		meta := readMeta(metaPath)
		if meta == nil {
			continue
		}
		if meta.Running {
			meta.Running = false
			meta.Orphaned = true
			writeMeta(metaPath, meta)
		}
		s.jobs[jobID] = &jobRecord{meta: meta, runtime: nil}
	}
}

func (s *Store) cleanupCompleted() {
	type item struct {
		jobID     string
		endedAtMs int64
	}
	var completed []item
	for jobID, rec := range s.jobs {
		if rec == nil || rec.meta == nil {
			continue
		}
		if rec.meta.Running {
			continue
		}
		if rec.meta.EndedAtMs == nil || *rec.meta.EndedAtMs <= 0 {
			continue
		}
		completed = append(completed, item{jobID: jobID, endedAtMs: *rec.meta.EndedAtMs})
	}
	sort.Slice(completed, func(i, j int) bool { return completed[i].endedAtMs > completed[j].endedAtMs })
	if len(completed) <= maxCompletedJobs {
		return
	}
	keep := make(map[string]struct{}, maxCompletedJobs)
	for _, it := range completed[:maxCompletedJobs] {
		keep[it.jobID] = struct{}{}
	}
	for _, it := range completed[maxCompletedJobs:] {
		if _, ok := keep[it.jobID]; ok {
			continue
		}
		s.deleteJob(it.jobID)
	}
}

func (s *Store) deleteJob(jobID string) {
	dir, _, _, _ := s.jobPaths(jobID)
	_ = os.RemoveAll(dir)
	delete(s.jobs, jobID)
}

func (s *Store) ListJobs() []interface{} {
	s.mu.Lock()
	defer s.mu.Unlock()

	out := make([]interface{}, 0, len(s.jobs))
	for jobID, rec := range s.jobs {
		if rec == nil || rec.meta == nil {
			continue
		}
		m := rec.meta
		argv := m.Argv
		out = append(out, listJob{
			JobID:     jobID,
			Running:   m.Running,
			Orphaned:  m.Orphaned,
			PID:       m.PID,
			Argv:      argv,
			Cwd:       m.Cwd,
			CreatedAt: &m.CreatedAtMs,
			StartedAt: m.StartedAtMs,
			EndedAt:   m.EndedAtMs,
			ExitCode:  m.ExitCode,
			Signal:    m.Signal,
			TimedOut:  m.TimedOut,
		})
	}

	sort.Slice(out, func(i, j int) bool {
		a := out[i].(listJob)
		b := out[j].(listJob)
		ar := 1
		if a.Running {
			ar = 0
		}
		br := 1
		if b.Running {
			br = 0
		}
		if ar != br {
			return ar < br
		}
		aT := int64(0)
		if a.StartedAt != nil && *a.StartedAt > 0 {
			aT = *a.StartedAt
		} else if a.CreatedAt != nil {
			aT = *a.CreatedAt
		}
		bT := int64(0)
		if b.StartedAt != nil && *b.StartedAt > 0 {
			bT = *b.StartedAt
		} else if b.CreatedAt != nil {
			bT = *b.CreatedAt
		}
		return bT < aT
	})

	return out
}

func (s *Store) GetJob(jobID string) *JobMeta {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec := s.jobs[jobID]
	if rec == nil || rec.meta == nil {
		return nil
	}
	cpy := *rec.meta
	return &cpy
}

func (s *Store) GetLogs(jobID string, tailBytes int) *logsPayload {
	tb := tailBytes
	if tb <= 0 {
		tb = defaultLogsTailBytes
	}
	tb = util.ClampInt(tb, 1024, 2*1024*1024)

	s.mu.Lock()
	rec := s.jobs[jobID]
	var meta *JobMeta
	var rt *JobRuntime
	if rec != nil {
		meta = rec.meta
		rt = rec.runtime
	}
	s.mu.Unlock()

	if meta == nil {
		return nil
	}

	if rt != nil {
		rt.mu.Lock()
		running := rt.running
		stdoutBytes := rt.stdoutBytes
		stderrBytes := rt.stderrBytes
		stdoutTail := rt.stdoutTail.Bytes()
		stderrTail := rt.stderrTail.Bytes()
		rt.mu.Unlock()
		if running {
			stdout := string(lastBytes(stdoutTail, tb))
			stderr := string(lastBytes(stderrTail, tb))
			return &logsPayload{
				JobID:            jobID,
				Running:          true,
				Stdout:           stdout,
				Stderr:           stderr,
				StdoutBytes:      stdoutBytes,
				StderrBytes:      stderrBytes,
				StdoutTruncated:  stdoutBytes > int64(rt.stdoutTail.Cap()),
				StderrTruncated:  stderrBytes > int64(rt.stderrTail.Cap()),
			}
		}
	}

	_, _, stdoutPath, stderrPath := s.jobPaths(jobID)
	stdout := readTailUTF8(stdoutPath, tb)
	stderr := readTailUTF8(stderrPath, tb)

	return &logsPayload{
		JobID:            jobID,
		Running:          false,
		Stdout:           stdout,
		Stderr:           stderr,
		StdoutBytes:      meta.StdoutBytes,
		StderrBytes:      meta.StderrBytes,
		StdoutTruncated:  meta.StdoutTruncated,
		StderrTruncated:  meta.StderrTruncated,
	}
}

func lastBytes(b []byte, n int) []byte {
	if n <= 0 {
		return nil
	}
	if len(b) <= n {
		return b
	}
	return b[len(b)-n:]
}

func readTailUTF8(filePath string, tailBytes int) string {
	st, err := os.Stat(filePath)
	if err != nil {
		return ""
	}
	size := st.Size()
	start := int64(0)
	if size > int64(tailBytes) {
		start = size - int64(tailBytes)
	}
	f, err := os.Open(filePath)
	if err != nil {
		return ""
	}
	defer f.Close()
	if start > 0 {
		if _, err := f.Seek(start, io.SeekStart); err != nil {
			return ""
		}
	}
	buf, err := io.ReadAll(f)
	if err != nil {
		return ""
	}
	return string(buf)
}

type RunParams struct {
	Argv        []string
	Cwd         *string
	Env         map[string]string
	TimeoutMs   *int
	YieldMs     *int
	NotifyOnExit *bool
	StdinText   *string
}

type RunResult struct {
	OK      bool
	Payload interface{}
	Error   *InvokeError
}

func (s *Store) Run(p RunParams) RunResult {
	argv := p.Argv
	if len(argv) == 0 {
		return RunResult{OK: false, Error: &InvokeError{Code: "BAD_INPUT", Message: "system.run requires argv: string[]"}}
	}
	if p.StdinText != nil && len([]byte(*p.StdinText)) > maxStdinTextBytes {
		return RunResult{OK: false, Error: &InvokeError{Code: "BAD_INPUT", Message: fmt.Sprintf("system.run stdinText too large (max %d bytes)", maxStdinTextBytes)}}
	}

	jobID, err := util.RandomUUID()
	if err != nil {
		return RunResult{OK: false, Error: &InvokeError{Code: "SPAWN_FAILED", Message: err.Error()}}
	}
	dir, metaPath, stdoutPath, stderrPath := s.jobPaths(jobID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return RunResult{OK: false, Error: &InvokeError{Code: "SPAWN_FAILED", Message: err.Error()}}
	}

	createdAtMs := util.NowUnixMs()
	notifyMode := "auto"
	if p.NotifyOnExit != nil {
		if *p.NotifyOnExit {
			notifyMode = "always"
		} else {
			notifyMode = "never"
		}
	}

	var stdinTextBytes *int
	if p.StdinText != nil {
		n := len([]byte(*p.StdinText))
		stdinTextBytes = &n
	}

	meta := &JobMeta{
		Version:          1,
		JobID:            jobID,
		NodeID:           s.nodeID,
		Argv:             argv,
		Cwd:              p.Cwd,
		StdinTextBytes:   stdinTextBytes,
		CreatedAtMs:      createdAtMs,
		StartedAtMs:      nil,
		EndedAtMs:        nil,
		PID:              nil,
		Running:          true,
		Orphaned:         false,
		TimeoutMs:        p.TimeoutMs,
		YieldMs:          p.YieldMs,
		NotifyOnExit:     notifyMode == "always",
		NotifyOnExitMode: notifyMode,
		ExitCode:         nil,
		Signal:           nil,
		TimedOut:         false,
		StdoutBytes:      0,
		StderrBytes:      0,
		StdoutTruncated:  false,
		StderrTruncated:  false,
	}
	writeMeta(metaPath, meta)

	cmd := exec.Command(argv[0], argv[1:]...)
	if p.Cwd != nil && strings.TrimSpace(*p.Cwd) != "" {
		cmd.Dir = strings.TrimSpace(*p.Cwd)
	}
	if p.Env != nil {
		env := os.Environ()
		for k, v := range p.Env {
			if strings.TrimSpace(k) == "" {
				continue
			}
			env = append(env, fmt.Sprintf("%s=%s", k, v))
		}
		cmd.Env = env
	}
	if p.StdinText != nil {
		cmd.Stdin = strings.NewReader(*p.StdinText)
	}

	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return RunResult{OK: false, Error: &InvokeError{Code: "SPAWN_FAILED", Message: err.Error()}}
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return RunResult{OK: false, Error: &InvokeError{Code: "SPAWN_FAILED", Message: err.Error()}}
	}

	if err := cmd.Start(); err != nil {
		return RunResult{OK: false, Error: &InvokeError{Code: "SPAWN_FAILED", Message: err.Error()}}
	}

	startedAtMs := util.NowUnixMs()
	meta.StartedAtMs = &startedAtMs
	if cmd.Process != nil {
		pid := cmd.Process.Pid
		meta.PID = &pid
	}
	writeMeta(metaPath, meta)

	rt := &JobRuntime{
		cmd:        cmd,
		running:    true,
		stdoutTail: NewTailBuffer(defaultLogTailBytes),
		stderrTail: NewTailBuffer(defaultLogTailBytes),
		doneCh:     make(chan struct{}),
	}

	rec := &jobRecord{meta: meta, runtime: rt}

	s.mu.Lock()
	s.jobs[jobID] = rec
	s.mu.Unlock()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		io.Copy(&tailWriter{rt: rt, which: "stdout"}, stdoutPipe)
	}()
	go func() {
		defer wg.Done()
		io.Copy(&tailWriter{rt: rt, which: "stderr"}, stderrPipe)
	}()

	if p.TimeoutMs != nil && *p.TimeoutMs > 0 {
		to := time.Duration(*p.TimeoutMs) * time.Millisecond
		rt.timeoutTimer = time.AfterFunc(to, func() {
			rt.mu.Lock()
			rt.timedOut = true
			rt.mu.Unlock()
			if cmd.Process != nil {
				_ = sendKill(cmd.Process)
			}
		})
	}

	go func() {
		defer close(rt.doneCh)
		err := cmd.Wait()
		if rt.timeoutTimer != nil {
			rt.timeoutTimer.Stop()
		}
		wg.Wait()

		exitCode, signal := exitInfo(cmd.ProcessState, err)

		rt.mu.Lock()
		rt.running = false
		stdoutBytes := rt.stdoutBytes
		stderrBytes := rt.stderrBytes
		stdoutTail := rt.stdoutTail.Bytes()
		stderrTail := rt.stderrTail.Bytes()
		timedOut := rt.timedOut
		rt.mu.Unlock()

		endedAtMs := util.NowUnixMs()

		s.mu.Lock()
		meta := rec.meta
		meta.Running = false
		meta.EndedAtMs = &endedAtMs
		meta.ExitCode = exitCode
		meta.Signal = signal
		meta.TimedOut = timedOut
		meta.StdoutBytes = stdoutBytes
		meta.StderrBytes = stderrBytes
		meta.StdoutTruncated = stdoutBytes > int64(rt.stdoutTail.Cap())
		meta.StderrTruncated = stderrBytes > int64(rt.stderrTail.Cap())
		writeMeta(metaPath, meta)
		_ = os.WriteFile(stdoutPath, stdoutTail, 0o644)
		_ = os.WriteFile(stderrPath, stderrTail, 0o644)
		rec.runtime = nil
		s.cleanupCompleted()
		notifyOnExit := meta.NotifyOnExit
		s.mu.Unlock()

		if notifyOnExit && s.sendEvent != nil {
			stdoutPreview := util.LastNRunes(string(stdoutTail), 4000)
			stderrPreview := util.LastNRunes(string(stderrTail), 4000)
			var sessionID interface{}
			if sid := strings.TrimSpace(os.Getenv("ARGUS_SESSION_ID")); sid != "" {
				sessionID = sid
			} else {
				sessionID = nil
			}
			s.sendEvent("node.process.exited", map[string]interface{}{
				"nodeId":      s.nodeID,
				"sessionId":   sessionID,
				"jobId":       jobID,
				"argv":        argv,
				"cwd":         p.Cwd,
				"exitCode":    exitCode,
				"signal":      signal,
				"timedOut":    timedOut,
				"startedAtMs": meta.StartedAtMs,
				"endedAtMs":   meta.EndedAtMs,
				"stdoutTail":  stdoutPreview,
				"stderrTail":  stderrPreview,
			})
		}
	}()

	ym := defaultYieldMs
	if p.YieldMs != nil {
		ym = util.ClampInt(*p.YieldMs, 0, 60*60*1000)
	}

	if ym == 0 {
		if notifyMode == "auto" && !meta.NotifyOnExit {
			s.mu.Lock()
			meta.NotifyOnExit = true
			writeMeta(metaPath, meta)
			s.mu.Unlock()
		}
		return RunResult{OK: true, Payload: s.runningPayload(jobID, rec, ym)}
	}

	select {
	case <-rt.doneCh:
		s.mu.Lock()
		meta := rec.meta
		s.mu.Unlock()

		stdout := string(rt.stdoutTail.Bytes())
		stderr := string(rt.stderrTail.Bytes())
		return RunResult{OK: true, Payload: runFinishedPayload{
			JobID:            jobID,
			Running:          false,
			Argv:             argv,
			Cwd:              p.Cwd,
			TimeoutMs:        p.TimeoutMs,
			YieldMs:          ym,
			PID:              meta.PID,
			ExitCode:         meta.ExitCode,
			Signal:           meta.Signal,
			TimedOut:         meta.TimedOut,
			Stdout:           stdout,
			Stderr:           stderr,
			StdoutBytes:      meta.StdoutBytes,
			StderrBytes:      meta.StderrBytes,
			StdoutTruncated:  meta.StdoutTruncated,
			StderrTruncated:  meta.StderrTruncated,
		}}
	case <-time.After(time.Duration(ym) * time.Millisecond):
		if notifyMode == "auto" && !meta.NotifyOnExit {
			s.mu.Lock()
			meta.NotifyOnExit = true
			writeMeta(metaPath, meta)
			s.mu.Unlock()
		}
		return RunResult{OK: true, Payload: s.runningPayload(jobID, rec, ym)}
	}
}

func (s *Store) runningPayload(jobID string, rec *jobRecord, ym int) runningPayload {
	var (
		argv        []string
		cwd         *string
		timeoutMs   *int
		yieldMs     *int
		pid         *int
		stdoutTail  string
		stderrTail  string
		stdoutBytes int64
		stderrBytes int64
	)
	s.mu.Lock()
	if rec != nil && rec.meta != nil {
		argv = rec.meta.Argv
		cwd = rec.meta.Cwd
		timeoutMs = rec.meta.TimeoutMs
		yieldMs = rec.meta.YieldMs
		pid = rec.meta.PID
	}
	rt := rec.runtime
	s.mu.Unlock()

	if rt != nil {
		rt.mu.Lock()
		stdoutTail = util.LastNRunes(string(rt.stdoutTail.Bytes()), 4000)
		stderrTail = util.LastNRunes(string(rt.stderrTail.Bytes()), 4000)
		stdoutBytes = rt.stdoutBytes
		stderrBytes = rt.stderrBytes
		rt.mu.Unlock()
	}

	return runningPayload{
		JobID:       jobID,
		Running:     true,
		Argv:        argv,
		Cwd:         cwd,
		TimeoutMs:   timeoutMs,
		YieldMs:     yieldMs,
		PID:         pid,
		StdoutTail:  stdoutTail,
		StderrTail:  stderrTail,
		StdoutBytes: stdoutBytes,
		StderrBytes: stderrBytes,
	}
}

func (s *Store) Kill(jobID string) RunResult {
	s.mu.Lock()
	rec := s.jobs[jobID]
	if rec == nil || rec.meta == nil {
		s.mu.Unlock()
		return RunResult{OK: false, Error: &InvokeError{Code: "NOT_FOUND", Message: fmt.Sprintf("unknown jobId: %s", jobID)}}
	}
	rt := rec.runtime
	s.mu.Unlock()
	if rt == nil || rt.cmd == nil || rt.cmd.Process == nil {
		return RunResult{OK: false, Error: &InvokeError{Code: "NOT_RUNNING", Message: fmt.Sprintf("job not running: %s", jobID)}}
	}

	_ = sendTerminate(rt.cmd.Process)
	time.Sleep(1500 * time.Millisecond)

	rt.mu.Lock()
	running := rt.running
	rt.mu.Unlock()
	if running {
		_ = sendKill(rt.cmd.Process)
	}
	return RunResult{OK: true, Payload: map[string]interface{}{"jobId": jobID, "signal": "SIGTERM"}}
}

type tailWriter struct {
	rt    *JobRuntime
	which string
}

func (w *tailWriter) Write(p []byte) (int, error) {
	if w == nil || w.rt == nil {
		return len(p), nil
	}
	w.rt.mu.Lock()
	defer w.rt.mu.Unlock()
	if w.which == "stdout" {
		w.rt.stdoutBytes += int64(len(p))
		w.rt.stdoutTail.Push(p)
	} else {
		w.rt.stderrBytes += int64(len(p))
		w.rt.stderrTail.Push(p)
	}
	return len(p), nil
}

func readMeta(filePath string) *JobMeta {
	b, err := os.ReadFile(filePath)
	if err != nil {
		return nil
	}
	var meta JobMeta
	if err := json.Unmarshal(b, &meta); err != nil {
		return nil
	}
	if strings.TrimSpace(meta.JobID) == "" {
		return nil
	}
	return &meta
}

func writeMeta(filePath string, meta *JobMeta) {
	if meta == nil {
		return
	}
	b, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return
	}
	_ = os.WriteFile(filePath, append(bytes.TrimRight(b, "\n"), '\n'), 0o644)
}

func EnvMap(v interface{}) map[string]string {
	m, ok := v.(map[string]interface{})
	if !ok || m == nil {
		return nil
	}
	out := make(map[string]string)
	for k, vv := range m {
		ks := strings.TrimSpace(k)
		vs, ok := vv.(string)
		if ks == "" || !ok {
			continue
		}
		out[ks] = vs
	}
	if len(out) == 0 {
		return nil
	}
	return out
}
