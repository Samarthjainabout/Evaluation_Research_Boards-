TOOLCHAIN_PATH ?= /usr/bin/
TOOLCHAIN_PREFIX ?= riscv64-unknown-elf
ARCH ?= rv32i_zicsr
PATTERN = gui_wb_mode

.SUFFIXES:

hex: $(PATTERN).hex

$(PATTERN).elf: $(PATTERN).c ../../sections.lds ../../crt0_vex.S
	$(TOOLCHAIN_PATH)$(TOOLCHAIN_PREFIX)-gcc -I../../ -I../../generated/ -O0 \
		-mabi=ilp32 -march=$(ARCH) -D__vexriscv__ \
		-Wl,-Bstatic,-T,../../sections.lds,--strip-debug -ffreestanding -nostdlib \
		-o $@ ../../crt0_vex.S ../../isr.c ../../stub.c $<
	$(TOOLCHAIN_PATH)$(TOOLCHAIN_PREFIX)-objdump -D $@ > $(PATTERN).lst

$(PATTERN).hex: $(PATTERN).elf
	$(TOOLCHAIN_PATH)$(TOOLCHAIN_PREFIX)-objcopy -O verilog $< $@
	sed -i 's/@1000/@0000/g' $@

clean:
	rm -f $(PATTERN).elf $(PATTERN).hex $(PATTERN).bin $(PATTERN).lst

.PHONY: clean hex
