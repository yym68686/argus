package jobstore

import "os"

func sendKill(p *os.Process) error {
	if p == nil {
		return nil
	}
	return p.Kill()
}

