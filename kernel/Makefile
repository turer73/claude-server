obj-m += proc_linux_ai.o
obj-m += nf_linux_ai.o
obj-m += usb_linux_ai.o

KDIR := /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

CPPCHECK_FLAGS := --inline-suppr --suppress=missingIncludeSystem \
	-DMODULE -D__KERNEL__ --std=c11 --quiet

all:
	make -C $(KDIR) M=$(PWD) modules

clean:
	make -C $(KDIR) M=$(PWD) clean

install: all
	sudo insmod proc_linux_ai.ko
	sudo insmod nf_linux_ai.ko
	sudo insmod usb_linux_ai.ko

uninstall:
	sudo rmmod usb_linux_ai 2>/dev/null || true
	sudo rmmod nf_linux_ai 2>/dev/null || true
	sudo rmmod proc_linux_ai 2>/dev/null || true

# Static analysis — CI gate (no style warnings, must exit clean)
lint:
	cppcheck --enable=warning,performance,portability --error-exitcode=1 $(CPPCHECK_FLAGS) *.c

# Full analysis — informational, includes opinionated style hints
lint-full:
	cppcheck --enable=warning,style,performance,portability $(CPPCHECK_FLAGS) *.c

.PHONY: all clean install uninstall lint lint-full
