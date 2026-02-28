package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"

	"github.com/yym68686/argus/apps/node-host-go/internal/jobstore"
	"github.com/yym68686/argus/apps/node-host-go/internal/nodehost"
)

func main() {
	cfg, err := nodehost.ParseConfig(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(2)
	}

	invoker := &nodehost.CommandInvoker{}
	host := nodehost.New(cfg, invoker)

	store, err := jobstore.New(cfg.StateDir, cfg.NodeID, host.SendEvent)
	if err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}
	invoker.Store = store

	ctx, stop := signal.NotifyContext(context.Background(), shutdownSignals()...)
	defer stop()

	if err := host.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}
}
