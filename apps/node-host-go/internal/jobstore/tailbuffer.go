package jobstore

type TailBuffer struct {
	capBytes int
	buf      []byte
	start    int
	size     int
}

func NewTailBuffer(capBytes int) *TailBuffer {
	if capBytes < 0 {
		capBytes = 0
	}
	return &TailBuffer{
		capBytes: capBytes,
		buf:      make([]byte, capBytes),
	}
}

func (t *TailBuffer) Cap() int { return t.capBytes }

func (t *TailBuffer) Len() int { return t.size }

func (t *TailBuffer) Push(p []byte) {
	if t == nil || t.capBytes <= 0 || len(p) == 0 {
		return
	}
	if len(p) >= t.capBytes {
		copy(t.buf, p[len(p)-t.capBytes:])
		t.start = 0
		t.size = t.capBytes
		return
	}

	overflow := t.size + len(p) - t.capBytes
	if overflow > 0 {
		t.start = (t.start + overflow) % t.capBytes
		t.size -= overflow
		if t.size < 0 {
			t.size = 0
		}
	}

	end := (t.start + t.size) % t.capBytes
	n := len(p)
	first := min(n, t.capBytes-end)
	copy(t.buf[end:end+first], p[:first])
	if first < n {
		copy(t.buf[0:n-first], p[first:])
	}
	t.size += n
	if t.size > t.capBytes {
		t.size = t.capBytes
	}
}

func (t *TailBuffer) Bytes() []byte {
	if t == nil || t.size <= 0 {
		return nil
	}
	out := make([]byte, t.size)
	if t.start+t.size <= t.capBytes {
		copy(out, t.buf[t.start:t.start+t.size])
		return out
	}
	first := t.capBytes - t.start
	copy(out, t.buf[t.start:])
	copy(out[first:], t.buf[:t.size-first])
	return out
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
