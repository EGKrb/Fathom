#include "hvkdpanel.h"

#ifdef EXTRACMDS_GUI

#include <QAbstractItemView>
#include <QApplication>
#include <QColor>
#include <QComboBox>
#include <QDir>
#include <QFileDialog>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QProcess>
#include <QPushButton>
#include <QSplitter>
#include <QTabWidget>
#include <QTableWidget>
#include <QVBoxLayout>

#include <functional>

namespace
{
    QString ToQString(const std::wstring & text)
    {
        return QString::fromWCharArray(text.c_str(), (int)text.size());
    }

    std::wstring ToWString(const QString & text)
    {
        std::wstring result((size_t)text.size(), L'\0');
        int n = text.toWCharArray(&result[0]);
        result.resize((size_t)n);
        return result;
    }

    // wineye emits every integer as a hex string ("0x4", "0xffff..."); accept decimal too.
    quint64 ParseU64(const QJsonValue & value)
    {
        if(value.isDouble())
            return (quint64)value.toDouble();
        QString s = value.toString().trimmed();
        bool ok = false;
        quint64 v = s.startsWith("0x", Qt::CaseInsensitive) ? s.mid(2).toULongLong(&ok, 16)
                                                             : s.toULongLong(&ok, 10);
        return ok ? v : 0;
    }

    QString Addr(const QJsonValue & value)
    {
        return QString("0x%1").arg(ParseU64(value), 16, 16, QChar('0'));
    }

    QString Dec(const QJsonValue & value)
    {
        return QString::number(ParseU64(value));
    }

    void SetHeaders(QTableWidget* table, const QStringList & headers)
    {
        table->setColumnCount(headers.size());
        table->setHorizontalHeaderLabels(headers);
        table->setEditTriggers(QAbstractItemView::NoEditTriggers);
        table->setSelectionBehavior(QAbstractItemView::SelectRows);
        table->setSelectionMode(QAbstractItemView::SingleSelection);
        table->setAlternatingRowColors(true);
        table->setSortingEnabled(true);
        table->verticalHeader()->setVisible(false);
        table->horizontalHeader()->setStretchLastSection(true);
    }
}

// ---------------------------------------------------------------------------
class HvKdPanel : public QWidget
{
    Q_OBJECT

public:
    explicit HvKdPanel(const HvKdContext & context)
        : mContext(context)
    {
        setWindowTitle(QStringLiteral("HvKdFusion"));
        setObjectName(QStringLiteral("HvKdFusionPanel"));
        mScript = ToQString(context.folder) + "\\hvkdfusion.py";
        buildUi();
        if(mContext.python.empty())
            mStatus->setText(statusHtml("orange", "Python introuvable sur le PATH — installez Python 3.11+"));
        else
            refresh();
    }

private:
    // -- UI ------------------------------------------------------------------
    void buildUi()
    {
        auto* root = new QVBoxLayout(this);

        // Target row: VM selector + refresh + open dump + overall status
        auto* targetBox = new QGroupBox(tr("Cible"), this);
        auto* targetRow = new QHBoxLayout(targetBox);
        targetRow->addWidget(new QLabel(tr("VM Hyper-V :"), targetBox));
        mVm = new QComboBox(targetBox);
        mVm->setMinimumWidth(220);
        mVm->setSizeAdjustPolicy(QComboBox::AdjustToContents);
        targetRow->addWidget(mVm);
        mRefresh = new QPushButton(tr("Rafraîchir"), targetBox);
        targetRow->addWidget(mRefresh);
        mOpenDump = new QPushButton(tr("Ouvrir un dump…"), targetBox);
        targetRow->addWidget(mOpenDump);
        targetRow->addStretch();
        mStatus = new QLabel(targetBox);
        mStatus->setTextFormat(Qt::RichText);
        targetRow->addWidget(mStatus);
        root->addWidget(targetBox);

        mTarget = new QLabel(tr("Cible : VM sélectionnée"), this);
        mTarget->setStyleSheet("color: gray;");
        root->addWidget(mTarget);

        auto* split = new QSplitter(Qt::Vertical, this);

        // Components readiness
        auto* compBox = new QGroupBox(tr("État des composants"), split);
        auto* compLayout = new QVBoxLayout(compBox);
        mComponents = new QTableWidget(compBox);
        SetHeaders(mComponents, { tr("État"), tr("Composant"), tr("Détail") });
        mComponents->setSortingEnabled(false);
        compLayout->addWidget(mComponents);
        mFeatures = new QLabel(compBox);
        mFeatures->setTextFormat(Qt::RichText);
        mFeatures->setWordWrap(true);
        compLayout->addWidget(mFeatures);
        mSetup = new QPushButton(tr("Lancer setup… (console)"), compBox);
        compLayout->addWidget(mSetup, 0, Qt::AlignLeft);
        split->addWidget(compBox);

        // Process explorer
        auto* procBox = new QGroupBox(tr("Processus"), split);
        auto* procLayout = new QVBoxLayout(procBox);
        auto* procTop = new QHBoxLayout();
        mLoadProc = new QPushButton(tr("Charger les processus"), procBox);
        procTop->addWidget(mLoadProc);
        procTop->addStretch();
        procLayout->addLayout(procTop);

        auto* procSplit = new QSplitter(Qt::Vertical, procBox);
        mProc = new QTableWidget(procSplit);
        SetHeaders(mProc, { "PID", "PPID", tr("Nom"), "Wow64", tr("Session"), tr("Intégrité"), "EPROCESS", "PEB" });
        procSplit->addWidget(mProc);

        mDetail = new QTabWidget(procSplit);
        mModules = new QTableWidget(mDetail);
        SetHeaders(mModules, { tr("Base"), tr("Taille"), tr("Module"), "Wow64", tr("Chemin") });
        mDetail->addTab(mModules, tr("Modules"));
        mVads = new QTableWidget(mDetail);
        SetHeaders(mVads, { tr("Début"), tr("Fin"), tr("Flags"), tr("Prot"), tr("Détail") });
        mDetail->addTab(mVads, tr("VAD"));
        mHandles = new QTableWidget(mDetail);
        SetHeaders(mHandles, { tr("Handle"), tr("Objet"), tr("Type"), tr("Détail") });
        mDetail->addTab(mHandles, tr("Handles"));
        procSplit->addWidget(mDetail);
        procLayout->addWidget(procSplit);
        split->addWidget(procBox);

        root->addWidget(split, 1);

        // Actions on the selected VM
        auto* actions = new QHBoxLayout();
        mDump = new QPushButton(tr("Dump…"), this);
        mDebug = new QPushButton(tr("Debug (WinDbg)"), this);
        mMount = new QPushButton(tr("Monter (MemProcFS)"), this);
        actions->addWidget(mDump);
        actions->addWidget(mDebug);
        actions->addWidget(mMount);
        actions->addStretch();
        root->addLayout(actions);

        connect(mRefresh, &QPushButton::clicked, this, [this] { refresh(); });
        connect(mOpenDump, &QPushButton::clicked, this, [this] { openDump(); });
        connect(mVm, QOverload<int>::of(&QComboBox::activated), this, [this](int) { mDumpPath.clear(); updateTarget(); });
        connect(mLoadProc, &QPushButton::clicked, this, [this] { loadProcesses(); });
        connect(mProc, &QTableWidget::itemSelectionChanged, this, [this] { onProcessSelected(); });
        connect(mDetail, &QTabWidget::currentChanged, this, [this](int) { loadCurrentDetail(); });
        connect(mSetup, &QPushButton::clicked, this, [this] { launch(L"setup"); });
        connect(mDump, &QPushButton::clicked, this, [this] { doDump(); });
        connect(mDebug, &QPushButton::clicked, this, [this] { doDebug(); });
        connect(mMount, &QPushButton::clicked, this, [this] { doMount(); });
    }

    static QString statusHtml(const QString & color, const QString & text)
    {
        return QString("<span style='color:%1;'>&#9679;</span> %2").arg(color, text.toHtmlEscaped());
    }

    // -- process runner (async; one JSON reader at a time) -------------------
    void setBusy(bool busy)
    {
        mBusy = busy;
        for(auto* b : { mRefresh, mLoadProc, mDump, mDebug, mMount, mOpenDump, mSetup })
            b->setEnabled(!busy);
        setCursor(busy ? Qt::BusyCursor : Qt::ArrowCursor);
    }

    // Run "python hvkdfusion.py <args>" and hand stdout to cb (empty on failure).
    void run(const QStringList & args, std::function<void(const QByteArray &)> cb)
    {
        if(mContext.python.empty())
        {
            cb(QByteArray());
            return;
        }
        setBusy(true);
        auto* proc = new QProcess(this);
        proc->setWorkingDirectory(ToQString(mContext.folder));
        proc->setProgram(ToQString(mContext.python));
        proc->setArguments(QStringList { "-X", "utf8", mScript } + args);
        connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
                [this, proc, cb](int, QProcess::ExitStatus)
        {
            QByteArray out = proc->readAllStandardOutput();
            proc->deleteLater();
            setBusy(false);
            cb(out);
        });
        connect(proc, &QProcess::errorOccurred, this, [this, proc, cb](QProcess::ProcessError)
        {
            proc->deleteLater();
            setBusy(false);
            cb(QByteArray());
        });
        proc->start();
    }

    // First line of out that parses as a JSON array (wineye ps/lm/!vad/!handle).
    static QJsonArray firstArray(const QByteArray & out)
    {
        for(const QByteArray & line : out.split('\n'))
        {
            QByteArray t = line.trimmed();
            if(t.startsWith('['))
            {
                QJsonDocument d = QJsonDocument::fromJson(t);
                if(d.isArray())
                    return d.array();
            }
        }
        return {};
    }

    static QJsonObject firstObject(const QByteArray & out)
    {
        for(const QByteArray & line : out.split('\n'))
        {
            QByteArray t = line.trimmed();
            if(t.startsWith('{'))
            {
                QJsonDocument d = QJsonDocument::fromJson(t);
                if(d.isObject())
                    return d.object();
            }
        }
        return {};
    }

    // -- target --------------------------------------------------------------
    QStringList targetArgs() const
    {
        if(!mDumpPath.isEmpty())
            return { "--dump", mDumpPath };
        if(mVm->currentIndex() >= 0 && mVm->currentData().isValid())
            return { "--live", "--vm", QString::number(mVm->currentData().toInt()) };
        return {};
    }

    void updateTarget()
    {
        if(!mDumpPath.isEmpty())
            mTarget->setText(tr("Cible : dump %1").arg(mDumpPath));
        else if(mVm->currentData().isValid())
            mTarget->setText(tr("Cible : VM %1 — %2 (live)").arg(mVm->currentData().toInt()).arg(mVm->currentText()));
        else
            mTarget->setText(tr("Cible : aucune (rafraîchissez ou ouvrez un dump)"));
    }

    void openDump()
    {
        QString file = QFileDialog::getOpenFileName(this, tr("Ouvrir une image mémoire / un crash dump"),
                       QString(), tr("Images mémoire (*.dmp *.raw *.mem *.vmem);;Tous les fichiers (*.*)"));
        if(file.isEmpty())
            return;
        mDumpPath = QDir::toNativeSeparators(file);
        updateTarget();
    }

    // -- refresh (check + list) ---------------------------------------------
    void refresh()
    {
        run({ "check", "--json" }, [this](const QByteArray & out) { fillCheck(firstObject(out)); });
        run({ "list", "--json" }, [this](const QByteArray & out) { fillList(firstObject(out)); });
    }

    void fillList(const QJsonObject & obj)
    {
        int keep = mVm->currentData().isValid() ? mVm->currentData().toInt() : -1;
        mVm->clear();
        QJsonArray vms = obj.value("vms").toArray();
        for(const QJsonValue & v : vms)
        {
            QJsonObject o = v.toObject();
            int id = (int)ParseU64(o.value("id"));
            mVm->addItem(QString("%1 — %2").arg(id).arg(o.value("name").toString()), id);
        }
        if(vms.isEmpty())
        {
            QString err = obj.value("error").toString();
            mVm->addItem(err.isEmpty() ? tr("(aucune VM — admin + VM démarrée requis)") : err);
            mVm->setCurrentIndex(0);
        }
        else if(keep >= 0)
        {
            int idx = mVm->findData(keep);
            if(idx >= 0)
                mVm->setCurrentIndex(idx);
        }
        updateTarget();
    }

    void fillCheck(const QJsonObject & obj)
    {
        QJsonArray comps = obj.value("components").toArray();
        mComponents->setRowCount(comps.size());
        int bad = 0, soft = 0;
        for(int i = 0; i < comps.size(); i++)
        {
            QJsonObject c = comps[i].toObject();
            QString st = c.value("status").toString();
            QString color = st == "ok" ? "#2ecc71" : st == "soft" ? "#f1c40f" : "#e74c3c";
            if(st == "bad")
                bad++;
            else if(st == "soft")
                soft++;
            auto* dot = new QTableWidgetItem(QString::fromUtf8("\xE2\x97\x8F")); // ●
            dot->setForeground(QColor(color));
            dot->setTextAlignment(Qt::AlignCenter);
            mComponents->setItem(i, 0, dot);
            mComponents->setItem(i, 1, new QTableWidgetItem(c.value("name").toString()));
            mComponents->setItem(i, 2, new QTableWidgetItem(c.value("detail").toString()));
        }
        mComponents->resizeColumnsToContents();
        mComponents->horizontalHeader()->setStretchLastSection(true);

        QStringList ready, missing;
        for(const QJsonValue & f : obj.value("features").toArray())
        {
            QJsonObject o = f.toObject();
            (o.value("ok").toBool() ? ready : missing) << o.value("name").toString();
        }
        QString feat = tr("<b>Prêt :</b> %1").arg(ready.isEmpty() ? tr("(rien)") : ready.join(", ").toHtmlEscaped());
        if(!missing.isEmpty())
            feat += tr("<br><b>Manquant :</b> %1").arg(missing.join(", ").toHtmlEscaped());
        mFeatures->setText(feat);

        if(bad)
            mStatus->setText(statusHtml("#e74c3c", tr("%1 composant(s) bloquant(s) manquant(s)").arg(bad)));
        else if(soft)
            mStatus->setText(statusHtml("#f1c40f", tr("prêt (%1 option(s) manquante(s))").arg(soft)));
        else
            mStatus->setText(statusHtml("#2ecc71", tr("tout est prêt")));
    }

    // -- processes -----------------------------------------------------------
    void loadProcesses()
    {
        QStringList target = targetArgs();
        if(target.isEmpty())
        {
            mTarget->setText(tr("Cible : aucune — choisissez une VM ou ouvrez un dump"));
            return;
        }
        runWineye("ps", [this](const QJsonArray & rows) { fillProcesses(rows); });
    }

    void runWineye(const QString & command, std::function<void(const QJsonArray &)> cb)
    {
        QStringList args = QStringList { "wineye" } + targetArgs() + QStringList { "-c", command, "--batch", "--json" };
        run(args, [cb](const QByteArray & out) { cb(firstArray(out)); });
    }

    void fillProcesses(const QJsonArray & rows)
    {
        mProc->setSortingEnabled(false);
        mProc->setRowCount(rows.size());
        for(int i = 0; i < rows.size(); i++)
        {
            QJsonObject p = rows[i].toObject();
            auto* pid = new QTableWidgetItem();
            pid->setData(Qt::DisplayRole, (qlonglong)ParseU64(p.value("pid")));
            mProc->setItem(i, 0, pid);
            auto* ppid = new QTableWidgetItem();
            ppid->setData(Qt::DisplayRole, (qlonglong)ParseU64(p.value("ppid")));
            mProc->setItem(i, 1, ppid);
            mProc->setItem(i, 2, new QTableWidgetItem(p.value("name").toString()));
            mProc->setItem(i, 3, new QTableWidgetItem(p.value("wow64").toBool() ? "✓" : ""));
            mProc->setItem(i, 4, new QTableWidgetItem(Dec(p.value("session"))));
            mProc->setItem(i, 5, new QTableWidgetItem(p.value("integrity").toString()));
            mProc->setItem(i, 6, new QTableWidgetItem(Addr(p.value("eprocess"))));
            mProc->setItem(i, 7, new QTableWidgetItem(Addr(p.value("peb"))));
        }
        mProc->setSortingEnabled(true);
        mProc->resizeColumnsToContents();
        mProc->horizontalHeader()->setStretchLastSection(true);
        mDetailPid = -1;
    }

    int selectedPid() const
    {
        auto items = mProc->selectedItems();
        if(items.isEmpty())
            return -1;
        return mProc->item(items.first()->row(), 0)->data(Qt::DisplayRole).toInt();
    }

    void onProcessSelected()
    {
        int pid = selectedPid();
        if(pid < 0 || pid == mDetailPid)
            return;
        mDetailPid = pid;
        mModulesLoaded = mVadsLoaded = mHandlesLoaded = false;
        mModules->setRowCount(0);
        mVads->setRowCount(0);
        mHandles->setRowCount(0);
        loadCurrentDetail();
    }

    void loadCurrentDetail()
    {
        if(mDetailPid < 0 || mBusy)
            return;
        int pid = mDetailPid;
        switch(mDetail->currentIndex())
        {
        case 0:
            if(!mModulesLoaded)
            {
                mModulesLoaded = true;
                runWineye(QString("lm %1").arg(pid), [this](const QJsonArray & a) { fillModules(a); });
            }
            break;
        case 1:
            if(!mVadsLoaded)
            {
                mVadsLoaded = true;
                runWineye(QString("!vad %1").arg(pid), [this](const QJsonArray & a) { fillVads(a); });
            }
            break;
        case 2:
            if(!mHandlesLoaded)
            {
                mHandlesLoaded = true;
                runWineye(QString("!handle %1").arg(pid), [this](const QJsonArray & a) { fillHandles(a); });
            }
            break;
        }
    }

    void fillModules(const QJsonArray & rows)
    {
        mModules->setSortingEnabled(false);
        mModules->setRowCount(rows.size());
        for(int i = 0; i < rows.size(); i++)
        {
            QJsonObject m = rows[i].toObject();
            mModules->setItem(i, 0, new QTableWidgetItem(Addr(m.value("base"))));
            mModules->setItem(i, 1, new QTableWidgetItem(Dec(m.value("size"))));
            mModules->setItem(i, 2, new QTableWidgetItem(m.value("name").toString()));
            mModules->setItem(i, 3, new QTableWidgetItem(m.value("wow64").toBool() ? "✓" : ""));
            mModules->setItem(i, 4, new QTableWidgetItem(m.value("path").toString()));
        }
        mModules->setSortingEnabled(true);
        mModules->resizeColumnsToContents();
        mModules->horizontalHeader()->setStretchLastSection(true);
    }

    void fillVads(const QJsonArray & rows)
    {
        mVads->setSortingEnabled(false);
        mVads->setRowCount(rows.size());
        for(int i = 0; i < rows.size(); i++)
        {
            QJsonObject v = rows[i].toObject();
            QString flags = QString(v.value("image").toBool() ? "I" : "") + (v.value("file").toBool() ? "F" : "")
                            + (v.value("stack").toBool() ? "S" : "");
            mVads->setItem(i, 0, new QTableWidgetItem(Addr(v.value("start"))));
            mVads->setItem(i, 1, new QTableWidgetItem(Addr(v.value("end"))));
            mVads->setItem(i, 2, new QTableWidgetItem(flags));
            mVads->setItem(i, 3, new QTableWidgetItem(Dec(v.value("protection"))));
            mVads->setItem(i, 4, new QTableWidgetItem(v.value("text").toString()));
        }
        mVads->setSortingEnabled(true);
        mVads->resizeColumnsToContents();
        mVads->horizontalHeader()->setStretchLastSection(true);
    }

    void fillHandles(const QJsonArray & rows)
    {
        mHandles->setSortingEnabled(false);
        mHandles->setRowCount(rows.size());
        for(int i = 0; i < rows.size(); i++)
        {
            QJsonObject h = rows[i].toObject();
            mHandles->setItem(i, 0, new QTableWidgetItem(Addr(h.value("handle"))));
            mHandles->setItem(i, 1, new QTableWidgetItem(Addr(h.value("object"))));
            mHandles->setItem(i, 2, new QTableWidgetItem(h.value("type").toString()));
            mHandles->setItem(i, 3, new QTableWidgetItem(h.value("text").toString()));
        }
        mHandles->setSortingEnabled(true);
        mHandles->resizeColumnsToContents();
        mHandles->horizontalHeader()->setStretchLastSection(true);
    }

    // -- elevated actions (hvkd.ps1 in a console) ----------------------------
    void launch(const std::wstring & args)
    {
        if(mContext.launch)
            mContext.launch(args);
    }

    bool currentVm(int & id) const
    {
        if(mDumpPath.isEmpty() && mVm->currentData().isValid())
            return id = mVm->currentData().toInt(), true;
        return false;
    }

    void doDump()
    {
        int id = 0;
        if(!currentVm(id))
        {
            mTarget->setText(tr("Dump : sélectionnez une VM (pas un dump déjà ouvert)"));
            return;
        }
        QString out = QFileDialog::getSaveFileName(this, tr("Enregistrer le dump"), QString("vm%1.dmp").arg(id),
                      tr("Crash dump (*.dmp);;Image brute (*.raw)"));
        if(out.isEmpty())
            return;
        QString type = out.endsWith(".raw", Qt::CaseInsensitive) ? "linear" : "crash";
        launch(ToWString(QString("dump --vm %1 --type %2 --out \"%3\"").arg(id).arg(type).arg(QDir::toNativeSeparators(out))));
    }

    void doDebug()
    {
        int id = 0;
        if(!currentVm(id))
        {
            mTarget->setText(tr("Debug : sélectionnez une VM"));
            return;
        }
        launch(ToWString(QString("debug --vm %1 --engine windbg").arg(id)));
    }

    void doMount()
    {
        if(!mDumpPath.isEmpty())
        {
            launch(ToWString(QString("mount --dump \"%1\"").arg(mDumpPath)));
            return;
        }
        int id = 0;
        if(!currentVm(id))
        {
            mTarget->setText(tr("Monter : sélectionnez une VM ou ouvrez un dump"));
            return;
        }
        launch(ToWString(QString("mount --from-vm %1").arg(id)));
    }

    HvKdContext mContext;
    QString mScript;
    QString mDumpPath;
    bool mBusy = false;
    int mDetailPid = -1;
    bool mModulesLoaded = false, mVadsLoaded = false, mHandlesLoaded = false;

    QComboBox* mVm = nullptr;
    QPushButton* mRefresh = nullptr;
    QPushButton* mOpenDump = nullptr;
    QPushButton* mLoadProc = nullptr;
    QPushButton* mSetup = nullptr;
    QPushButton* mDump = nullptr;
    QPushButton* mDebug = nullptr;
    QPushButton* mMount = nullptr;
    QLabel* mStatus = nullptr;
    QLabel* mTarget = nullptr;
    QLabel* mFeatures = nullptr;
    QTableWidget* mComponents = nullptr;
    QTableWidget* mProc = nullptr;
    QTabWidget* mDetail = nullptr;
    QTableWidget* mModules = nullptr;
    QTableWidget* mVads = nullptr;
    QTableWidget* mHandles = nullptr;
};

QWidget* CreateHvKdPanel(const HvKdContext & context)
{
    if(context.folder.empty())
        return nullptr;
    // No GUI in headless.exe: creating a QWidget without a QApplication would crash.
    if(!qobject_cast<QApplication*>(QCoreApplication::instance()))
        return nullptr;
    return new HvKdPanel(context);
}

#include "hvkdpanel.moc"

#endif // EXTRACMDS_GUI
