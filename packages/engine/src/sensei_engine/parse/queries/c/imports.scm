; C includes. @source: the path, unquoted for a local header (#include "util/log.h": util/log.h),
; as written for a system one (#include <stdio.h>: <stdio.h>). A local include is looked up
; next to the file first; a system one on the include path only. An include of a macro
; (#include CONFIG_H) isn't captured.

(preproc_include
  path: (string_literal
    (string_content) @source)) @import.local

(preproc_include
  path: (system_lib_string) @source) @import.system
