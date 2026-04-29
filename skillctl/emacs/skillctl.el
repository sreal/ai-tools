;;; skillctl.el --- Manage Claude/Codex skills via skillctl  -*- lexical-binding: t; -*-

;; Author: skillctl contributors
;; Keywords: tools, convenience
;; Package-Requires: ((emacs "28.1"))

;;; Commentary:

;; Emacs front-end for the `skillctl' CLI: list installed skills across
;; configured homes and projects, install/remove from a hand-managed vault,
;; trigger scans, and view the resolved config.
;;
;; Quick start:
;;
;;   (load-file "/path/to/skillctl/emacs/skillctl.el")
;;   ;; If skillctl isn't on PATH:
;;   (setq skillctl-program '("uv" "run" "/path/to/skillctl/skillctl.py"))
;;   M-x skillctl-list
;;
;; Keys in `skillctl-list-mode' / `skillctl-vault-list-mode':
;;
;;   g   refresh
;;   i   install skill at point (or prompt)
;;   d   remove skill at point
;;   RET open the skill's SKILL.md
;;   s   trigger a scan
;;   v   open the vault buffer
;;   c   show config + targets
;;   q   quit window

;;; Code:

(require 'json)
(require 'subr-x)
(require 'tabulated-list)

;; ---------------------------------------------------------------------------
;; Customization
;; ---------------------------------------------------------------------------

(defgroup skillctl nil
  "Manage Claude/Codex skills via the skillctl CLI."
  :group 'tools
  :prefix "skillctl-")

(defcustom skillctl-program "skillctl"
  "Program used to invoke skillctl.
A string is the executable name (resolved via `executable-find').
A list is used directly as the argv prefix, e.g.
\\='(\"uv\" \"run\" \"/path/to/skillctl.py\")."
  :type '(choice (string :tag "Executable name")
                 (repeat :tag "Argv prefix" string))
  :group 'skillctl)

(defcustom skillctl-config-file nil
  "Optional path passed to skillctl via --config.
When nil, skillctl resolves the config path itself."
  :type '(choice (const :tag "Default (skillctl resolves)" nil)
                 (file :tag "Path to config.json"))
  :group 'skillctl)

(defcustom skillctl-process-buffer-name "*skillctl-process*"
  "Name of the buffer used for asynchronous skillctl output."
  :type 'string
  :group 'skillctl)

;; ---------------------------------------------------------------------------
;; Internal helpers
;; ---------------------------------------------------------------------------

(defun skillctl--program-argv ()
  "Return the argv prefix from `skillctl-program' as a list."
  (cond
   ((stringp skillctl-program)
    (let ((resolved (executable-find skillctl-program)))
      (unless resolved
        (user-error "Cannot find `%s' on PATH; customize `skillctl-program'"
                    skillctl-program))
      (list resolved)))
   ((and (listp skillctl-program) skillctl-program)
    skillctl-program)
   (t (user-error "`skillctl-program' must be a string or non-empty list"))))

(defun skillctl--argv (subargs &optional include-json)
  "Build full argv: program + global flags + SUBARGS.
When INCLUDE-JSON is non-nil, prepend --json before the subcommand."
  (let ((globals nil))
    (when skillctl-config-file
      (setq globals (append globals (list "--config"
                                          (expand-file-name skillctl-config-file)))))
    (when include-json
      (setq globals (append globals (list "--json"))))
    (append (skillctl--program-argv) globals subargs)))

(defun skillctl--call-json (subargs &optional allowed-exit-codes)
  "Run skillctl with --json + SUBARGS synchronously and return parsed JSON.
ALLOWED-EXIT-CODES is a list of non-zero exit codes that should not signal
a `user-error' (the parsed JSON is returned anyway).  On other non-zero
exits, signals `user-error' with stderr text."
  (let* ((argv (skillctl--argv subargs t))
         (prog (car argv))
         (args (cdr argv))
         (stderr-file (make-temp-file "skillctl-stderr-")))
    (unwind-protect
        (with-temp-buffer
          (let* ((exit (apply #'call-process prog nil
                              (list (current-buffer) stderr-file)
                              nil args)))
            (cond
             ((eq exit 0)
              (goto-char (point-min))
              (if (eobp)
                  nil
                (json-parse-buffer :object-type 'alist
                                   :array-type 'list
                                   :false-object nil
                                   :null-object nil)))
             ((memq exit allowed-exit-codes)
              (goto-char (point-min))
              (let ((parsed (if (eobp) nil
                              (json-parse-buffer :object-type 'alist
                                                 :array-type 'list
                                                 :false-object nil
                                                 :null-object nil))))
                (cons exit parsed)))
             (t
              (let ((stderr-text (with-temp-buffer
                                   (insert-file-contents stderr-file)
                                   (string-trim (buffer-string)))))
                (user-error "skillctl exited %d: %s"
                            exit
                            (if (string-empty-p stderr-text)
                                "(no stderr)"
                              stderr-text)))))))
      (ignore-errors (delete-file stderr-file)))))

(defun skillctl--run-async (subargs on-finish)
  "Run skillctl with SUBARGS asynchronously, streaming output to a buffer.
ON-FINISH is a function called with the process exit code."
  (let* ((argv (skillctl--argv subargs nil))
         (buf (get-buffer-create skillctl-process-buffer-name))
         (cmd-string (mapconcat #'shell-quote-argument argv " ")))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (goto-char (point-max))
        (insert (format "\n$ %s\n" cmd-string)))
      (special-mode))
    (display-buffer buf)
    (make-process
     :name "skillctl"
     :buffer buf
     :command argv
     :noquery t
     :sentinel
     (lambda (proc _event)
       (when (memq (process-status proc) '(exit signal))
         (let ((code (process-exit-status proc)))
           (with-current-buffer (process-buffer proc)
             (let ((inhibit-read-only t))
               (goto-char (point-max))
               (insert (format "[exit %d]\n" code))))
           (when on-finish (funcall on-finish code))))))))

(defun skillctl--refresh-open-buffers ()
  "Refresh any open `skillctl-list-mode' or `skillctl-vault-list-mode' buffers."
  (dolist (buf (buffer-list))
    (with-current-buffer buf
      (when (derived-mode-p 'skillctl-list-mode 'skillctl-vault-list-mode)
        (revert-buffer nil t)))))

;; ---------------------------------------------------------------------------
;; Data fetchers
;; ---------------------------------------------------------------------------

(defun skillctl--fetch-list ()
  "Return the parsed JSON list of installed skills."
  (or (skillctl--call-json '("list")) '()))

(defun skillctl--fetch-vault ()
  "Return the parsed JSON list of vault entries."
  (or (skillctl--call-json '("vault" "list")) '()))

(defun skillctl--fetch-config ()
  "Return the parsed config-show object."
  (skillctl--call-json '("config" "show")))

(defun skillctl--vault-skill-names ()
  "Distinct skill names available in the vault."
  (delete-dups
   (mapcar (lambda (row) (alist-get 'skill row))
           (skillctl--fetch-vault))))

(defun skillctl--vault-versions-for (skill)
  "All versions present in the vault for SKILL."
  (mapcar (lambda (row) (alist-get 'version row))
          (seq-filter (lambda (row) (equal (alist-get 'skill row) skill))
                      (skillctl--fetch-vault))))

(defun skillctl--installed-skill-names ()
  "Distinct skill names currently installed."
  (delete-dups
   (mapcar (lambda (row) (alist-get 'name row))
           (skillctl--fetch-list))))

(defun skillctl--known-projects ()
  "Project paths from the resolved config."
  (let* ((cfg (skillctl--fetch-config))
         (inner (alist-get 'config cfg)))
    (alist-get 'projects inner)))

;; ---------------------------------------------------------------------------
;; skillctl-list-mode
;; ---------------------------------------------------------------------------

(defvar-keymap skillctl-list-mode-map
  :doc "Keymap for `skillctl-list-mode'."
  :parent tabulated-list-mode-map
  "g" #'tabulated-list-revert
  "i" #'skillctl-install
  "d" #'skillctl-remove-at-point
  "RET" #'skillctl-find-skill-md
  "s" #'skillctl-scan
  "v" #'skillctl-vault-list
  "c" #'skillctl-config-show
  "q" #'quit-window)

(define-derived-mode skillctl-list-mode tabulated-list-mode "skillctl"
  "Major mode for browsing installed Claude/Codex skills."
  (setq tabulated-list-format
        [("scope"    8  t)
         ("layout"   7  t)
         ("name"    22  t)
         ("version" 10  t)
         ("source"  10  t)
         ("location" 30 t)
         ("path"    40  t)])
  (setq tabulated-list-padding 1)
  (setq tabulated-list-sort-key (cons "name" nil))
  (setq-local revert-buffer-function #'skillctl--list-revert)
  (tabulated-list-init-header))

(defun skillctl--list-revert (&optional _arg _noconfirm)
  "Re-fetch and re-render the installed-skills tabulated list."
  (let ((rows (skillctl--fetch-list)))
    (setq tabulated-list-entries
          (mapcar
           (lambda (r)
             (let ((path (alist-get 'path r)))
               (list path
                     (vector
                      (or (alist-get 'scope r) "")
                      (or (alist-get 'layout r) "")
                      (or (alist-get 'name r) "")
                      (or (alist-get 'version r) "")
                      (or (alist-get 'source r) "")
                      (or (alist-get 'location r) "")
                      (or path "")))))
           rows))
    (tabulated-list-print t)))

(defun skillctl--row-at-point ()
  "Return the alist for the row at point in a list/vault buffer, or nil."
  (let ((id (tabulated-list-get-id))
        (entry (tabulated-list-get-entry)))
    (when (and id entry)
      (cond
       ((derived-mode-p 'skillctl-list-mode)
        ;; columns: scope layout name version source location path
        `((scope    . ,(aref entry 0))
          (layout   . ,(aref entry 1))
          (name     . ,(aref entry 2))
          (version  . ,(aref entry 3))
          (source   . ,(aref entry 4))
          (location . ,(aref entry 5))
          (path     . ,(aref entry 6))))
       ((derived-mode-p 'skillctl-vault-list-mode)
        ;; columns: skill version default path
        `((skill   . ,(aref entry 0))
          (version . ,(aref entry 1))
          (default . ,(aref entry 2))
          (path    . ,(aref entry 3))))))))

;; ---------------------------------------------------------------------------
;; skillctl-vault-list-mode
;; ---------------------------------------------------------------------------

(defvar-keymap skillctl-vault-list-mode-map
  :doc "Keymap for `skillctl-vault-list-mode'."
  :parent tabulated-list-mode-map
  "g" #'tabulated-list-revert
  "i" #'skillctl-install-at-point
  "RET" #'skillctl-find-skill-md
  "v" #'skillctl-list
  "q" #'quit-window)

(define-derived-mode skillctl-vault-list-mode tabulated-list-mode "skillctl-vault"
  "Major mode for browsing the skillctl vault."
  (setq tabulated-list-format
        [("skill"   22 t)
         ("version" 12 t)
         ("default"  7 t)
         ("path"    60 t)])
  (setq tabulated-list-padding 1)
  (setq tabulated-list-sort-key (cons "skill" nil))
  (setq-local revert-buffer-function #'skillctl--vault-revert)
  (tabulated-list-init-header))

(defun skillctl--vault-revert (&optional _arg _noconfirm)
  "Re-fetch and re-render the vault tabulated list."
  (let ((rows (skillctl--fetch-vault)))
    (setq tabulated-list-entries
          (mapcar
           (lambda (r)
             (let ((path (alist-get 'path r)))
               (list path
                     (vector
                      (or (alist-get 'skill r) "")
                      (or (alist-get 'version r) "")
                      (if (alist-get 'default r) "*" "")
                      (or path "")))))
           rows))
    (tabulated-list-print t)))

;; ---------------------------------------------------------------------------
;; Interactive commands
;; ---------------------------------------------------------------------------

;;;###autoload
(defun skillctl-list ()
  "Open a buffer listing installed skills across configured homes and projects."
  (interactive)
  (let ((buf (get-buffer-create "*skillctl-list*")))
    (with-current-buffer buf
      (skillctl-list-mode)
      (revert-buffer nil t))
    (pop-to-buffer buf)))

;;;###autoload
(defun skillctl-vault-list ()
  "Open a buffer listing the contents of the skillctl vault."
  (interactive)
  (let ((buf (get-buffer-create "*skillctl-vault*")))
    (with-current-buffer buf
      (skillctl-vault-list-mode)
      (revert-buffer nil t))
    (pop-to-buffer buf)))

(defun skillctl--read-install-args (default-name default-version)
  "Read install arguments from the minibuffer.
DEFAULT-NAME and DEFAULT-VERSION pre-fill the prompts when non-nil.
With one prefix arg, also prompts for a version.  With two, also for a project."
  (let* ((vault-names (skillctl--vault-skill-names))
         (name (completing-read
                (format-prompt "Install skill" default-name)
                vault-names nil nil nil nil default-name))
         (versions (skillctl--vault-versions-for name))
         (version (cond
                   ((>= (prefix-numeric-value current-prefix-arg) 4)
                    (let ((v (completing-read
                              (format-prompt "Version" (or default-version "default"))
                              versions nil nil nil nil
                              (or default-version ""))))
                      (if (string-empty-p v) nil v)))
                   (t default-version)))
         (project (when (>= (prefix-numeric-value current-prefix-arg) 16)
                    (let ((p (completing-read
                              (format-prompt "Project (empty=fan-out)" "")
                              (skillctl--known-projects) nil nil)))
                      (if (string-empty-p p) nil p))))
         (force (yes-or-no-p "Pass --force? "))
         (skill-spec (if version (concat name "@" version) name)))
    (list skill-spec project force)))

;;;###autoload
(defun skillctl-install (skill-spec project force)
  "Install SKILL-SPEC (NAME or NAME@VERSION) via skillctl.
With prefix arg, also prompt for version; with double prefix, also for PROJECT.
FORCE non-nil passes --force."
  (interactive (skillctl--read-install-args nil nil))
  (let* ((subargs (append (list "install" skill-spec)
                          (when project (list "--project" project))
                          (when force (list "--force"))))
         (on-finish
          (lambda (code)
            (skillctl--refresh-open-buffers)
            (cond
             ((eq code 0)
              (message "skillctl install %s: ok" skill-spec))
             ((eq code 2)
              (when (yes-or-no-p
                     (format "skillctl: %s already installed; retry with --force? "
                             skill-spec))
                (skillctl-install skill-spec project t)))
             (t
              (message "skillctl install %s exited %d (see %s)"
                       skill-spec code skillctl-process-buffer-name))))))
    (skillctl--run-async subargs on-finish)))

(defun skillctl-install-at-point ()
  "Install the skill on the current vault row."
  (interactive)
  (let* ((row (skillctl--row-at-point)))
    (unless row (user-error "No skill row at point"))
    (let* ((name (alist-get 'skill row))
           (version (alist-get 'version row))
           (force (yes-or-no-p "Pass --force? "))
           (spec (concat name "@" version)))
      (skillctl-install spec nil force))))

;;;###autoload
(defun skillctl-remove (skill project)
  "Remove SKILL from configured destinations (or PROJECT if non-nil)."
  (interactive
   (let* ((installed (skillctl--installed-skill-names))
          (default (when (derived-mode-p 'skillctl-list-mode)
                     (alist-get 'name (skillctl--row-at-point))))
          (name (completing-read
                 (format-prompt "Remove skill" default)
                 installed nil nil nil nil default))
          (project (when (>= (prefix-numeric-value current-prefix-arg) 4)
                     (let ((p (completing-read
                               (format-prompt "Project (empty=fan-out)" "")
                               (skillctl--known-projects) nil nil)))
                       (if (string-empty-p p) nil p)))))
     (list name project)))
  (when (yes-or-no-p (format "Really remove `%s'%s? "
                             skill
                             (if project (format " from %s" project) "")))
    (let ((subargs (append (list "remove" skill)
                           (when project (list "--project" project)))))
      (skillctl--run-async
       subargs
       (lambda (code)
         (skillctl--refresh-open-buffers)
         (message "skillctl remove %s: exit %d" skill code))))))

(defun skillctl-remove-at-point ()
  "Remove the skill on the current row."
  (interactive)
  (let* ((row (skillctl--row-at-point)))
    (unless row (user-error "No skill row at point"))
    (let* ((name (alist-get 'name row))
           (scope (alist-get 'scope row))
           (location (alist-get 'location row))
           (project (when (equal scope "project") location)))
      (skillctl-remove name project))))

;;;###autoload
(defun skillctl-scan ()
  "Run `skillctl scan' and refresh any open list buffers."
  (interactive)
  (let* ((result (skillctl--call-json '("scan")))
         (projects (alist-get 'projects result)))
    (skillctl--refresh-open-buffers)
    (message "skillctl scan: %d project%s found"
             (length projects)
             (if (= (length projects) 1) "" "s"))))

;;;###autoload
(defun skillctl-vault-set (path)
  "Set the skillctl vault directory to PATH."
  (interactive (list (read-directory-name "Vault path: ")))
  (skillctl--call-json (list "vault" "set" (expand-file-name path)))
  (skillctl--refresh-open-buffers)
  (message "skillctl vault set: %s" path))

;;;###autoload
(defun skillctl-config-show ()
  "Open a buffer showing the resolved skillctl config and target paths."
  (interactive)
  (let* ((cfg (skillctl--fetch-config))
         (buf (get-buffer-create "*skillctl-config*")))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert (json-encode cfg))
        (goto-char (point-min))
        (when (fboundp 'json-pretty-print-buffer)
          (json-pretty-print-buffer))
        (goto-char (point-min)))
      (special-mode))
    (pop-to-buffer buf)))

(defun skillctl-find-skill-md ()
  "Open the SKILL.md file for the row at point in another window."
  (interactive)
  (let* ((row (skillctl--row-at-point))
         (path (and row (alist-get 'path row))))
    (unless (and path (file-directory-p path))
      (user-error "No skill directory at point"))
    (let ((skill-file (expand-file-name "SKILL.md" path)))
      (unless (file-readable-p skill-file)
        (user-error "SKILL.md not readable: %s" skill-file))
      (find-file-other-window skill-file))))

(provide 'skillctl)

;;; skillctl.el ends here
