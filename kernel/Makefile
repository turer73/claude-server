obj-m += proc_linux_ai.o
obj-m += nf_linux_ai.o
obj-m += usb_linux_ai.o

KDIR := /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

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

.PHONY: all clean install uninstall
