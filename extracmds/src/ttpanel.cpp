#include "ttpanel.h"

#ifdef EXTRACMDS_GUI

#include "_plugins.h"
#include "ttmodel.h"

#include <QAbstractItemView>
#include <QCheckBox>
#include <QComboBox>
#include <QFileDialog>
#include <QFontDatabase>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QMouseEvent>
#include <QPainter>
#include <QPlainTextEdit>
#include <QPointer>
#include <QPushButton>
#include <QSpinBox>
#include <QSplitter>
#include <QTableWidget>
#include <QVBoxLayout>

#include <atomic>
#include <climits>
#include <functional>

namespace
{
    QString FromUtf8(const std::string & text)
    {
        return QString::fromUtf8(text.c_str(), (int)text.size());
    }

    QString Hex(uint64_t value, unsigned pointerSize)
    {
        return QStringLiteral("%1").arg((qulonglong)value, (int)pointerSize * 2, 16, QChar('0')).toUpper();
    }

    QColor ThreadColor(int index)
    {
        return QColor::fromHsv((index * 67) % 360, 140, 210);
    }

    // Per-thread timelines: one lane per thread, the spans where it runs, the position as a red line.
    // A click on a lane at instant T shows that thread at T.
    class TtTimeline : public QWidget
    {
    public:
        std::function<void(uint32_t thread, uint64_t seq)> onClick;

        explicit TtTimeline(QWidget* parent) : QWidget(parent)
        {
            setMinimumHeight(30);
            setCursor(Qt::PointingHandCursor);
            setToolTip(tr("Chronologies par thread : cliquer sur une piste pour voir ce thread à cet instant"));
        }

        void setData(uint64_t count, const std::vector<ftt::ThreadInfo> & threads, const std::vector<std::vector<std::pair<uint64_t, uint64_t>>> & ranges,
                     uint64_t position, uint32_t viewThread)
        {
            mCount = count;
            mThreads = threads;
            mRanges = ranges;
            mPosition = position;
            mViewThread = viewThread;
            setMinimumHeight(LaneHeight * std::max<int>(1, (int)threads.size()) + 6);
            update();
        }

        QSize sizeHint() const override
        {
            return QSize(600, LaneHeight * std::max<int>(1, (int)mThreads.size()) + 6);
        }

    protected:
        void paintEvent(QPaintEvent*) override
        {
            QPainter painter(this);
            painter.fillRect(rect(), palette().base());
            if(!mCount)
            {
                painter.drawText(rect(), Qt::AlignCenter, tr("Aucune trace ouverte"));
                return;
            }
            const int left = LabelWidth, width = std::max(1, this->width() - LabelWidth - 4);
            for(size_t i = 0; i < mThreads.size(); i++)
            {
                const int top = 3 + (int)i * LaneHeight;
                const bool viewed = mThreads[i].thread == mViewThread;
                if(viewed)
                    painter.fillRect(QRect(0, top, this->width(), LaneHeight - 2), palette().alternateBase());
                QFont font = painter.font();
                font.setBold(viewed);
                painter.setFont(font);
                painter.setPen(palette().text().color());
                painter.drawText(QRect(2, top, LabelWidth - 6, LaneHeight - 2), Qt::AlignVCenter | Qt::AlignRight, QString::number(mThreads[i].thread));
                const QColor color = ThreadColor((int)i);
                for(auto & r : mRanges[i])
                {
                    const int x1 = left + (int)((double)r.first / mCount * width);
                    const int x2 = left + (int)((double)(r.second + 1) / mCount * width);
                    painter.fillRect(QRect(x1, top + 3, std::max(1, x2 - x1), LaneHeight - 8), color);
                }
            }
            const int x = left + (int)((double)mPosition / mCount * width);
            painter.setPen(QPen(Qt::red, 2));
            painter.drawLine(x, 0, x, height());
        }

        void mousePressEvent(QMouseEvent* event) override
        {
            if(!mCount || mThreads.empty() || !onClick)
                return;
            const int lane = std::min<int>((int)mThreads.size() - 1, std::max(0, (event->pos().y() - 3) / LaneHeight));
            const int width = std::max(1, this->width() - LabelWidth - 4);
            const double ratio = std::min(1.0, std::max(0.0, (double)(event->pos().x() - LabelWidth) / width));
            onClick(mThreads[lane].thread, std::min<uint64_t>(mCount - 1, (uint64_t)(ratio * mCount)));
        }

    private:
        static constexpr int LaneHeight = 18;
        static constexpr int LabelWidth = 70;
        uint64_t mCount = 0;
        std::vector<ftt::ThreadInfo> mThreads;
        std::vector<std::vector<std::pair<uint64_t, uint64_t>>> mRanges;
        uint64_t mPosition = 0;
        uint32_t mViewThread = 0;
    };

    class TimeTravelPanel :
        public QWidget
    {
    public:
        TimeTravelPanel()
        {
            setWindowTitle(QStringLiteral("Time Travel"));
            setObjectName(QStringLiteral("TimeTravelPanel"));
            const QFont mono = QFontDatabase::systemFont(QFontDatabase::FixedFont);
            auto* root = new QVBoxLayout(this);

            // file and position
            auto* fileRow = new QHBoxLayout();
            auto* open = new QPushButton(tr("Ouvrir une trace…"), this);
            open->setToolTip(tr(".ftt (Fathom Time Travel), ou .trace64 / .trace32 enregistrée par x64dbg (convertie)"));
            mFile = new QLabel(tr("Aucune trace ouverte"), this);
            mFile->setTextInteractionFlags(Qt::TextSelectableByMouse);
            fileRow->addWidget(open);
            fileRow->addWidget(mFile, 1);
            root->addLayout(fileRow);

            auto* navRow = new QHBoxLayout();
            auto button = [this, navRow](const QString & text, const QString & tip, std::function<void()> action)
            {
                auto* b = new QPushButton(text, this);
                b->setToolTip(tip);
                connect(b, &QPushButton::clicked, this, [action] { action(); });
                navRow->addWidget(b);
                return b;
            };
            auto & model = TtModel::Get();
            button(QStringLiteral("|◀"), tr("Début de la trace"), [&model] { model.Seek(0); });
            button(QStringLiteral("◀◀"), tr("Instruction précédente (tous threads)"), [&model] { model.Step(false, false); });
            button(QStringLiteral("◀"), tr("Instruction précédente du thread affiché"), [&model] { model.Step(false, true); });
            button(QStringLiteral("▶"), tr("Instruction suivante du thread affiché"), [&model] { model.Step(true, true); });
            button(QStringLiteral("▶▶"), tr("Instruction suivante (tous threads)"), [&model] { model.Step(true, false); });
            button(QStringLiteral("▶|"), tr("Fin de la trace"), [&model] { auto n = model.GetInfo().count; if(n) model.Seek(n - 1); });
            navRow->addSpacing(12);
            navRow->addWidget(new QLabel(tr("Instruction n°"), this));
            mPosition = new QSpinBox(this);
            mPosition->setRange(0, 0);
            mPosition->setKeyboardTracking(false);
            mPosition->setMinimumWidth(110);
            connect(mPosition, QOverload<int>::of(&QSpinBox::valueChanged), this, [this](int value)
            {
                if(!mUpdating)
                    TtModel::Get().Seek((uint64_t)value);
            });
            navRow->addWidget(mPosition);
            mWhere = new QLabel(this);
            navRow->addWidget(mWhere, 1);
            mShowCpu = button(tr("Afficher dans le CPU"), tr("Montrer l'adresse dans la vue CPU (débogage du processus de la trace)"), [this]
            {
                ftt::Instruction ins;
                if(TtModel::Get().Instruction(TtModel::Get().Position(), ins) && TtModel::Get().SameProcess())
                    GuiDisasmAt((duint)ins.ip, (duint)ins.ip);
            });
            root->addLayout(navRow);

            mTimeline = new TtTimeline(this);
            mTimeline->onClick = [](uint32_t thread, uint64_t seq)
            {
                TtModel::Get().Seek(seq);
                TtModel::Get().SwitchThread(thread);
            };
            root->addWidget(mTimeline);

            // search: execution of an address, writes to memory
            auto* searchRow = new QHBoxLayout();
            searchRow->addWidget(new QLabel(tr("Adresse :"), this));
            mSearch = new QLineEdit(this);
            mSearch->setPlaceholderText(tr("adresse, ou registre (rip, rsp…)"));
            mSearch->setMaximumWidth(220);
            searchRow->addWidget(mSearch);
            auto searchButton = [this, searchRow](const QString & text, const QString & tip, std::function<bool(uint64_t)> action)
            {
                auto* b = new QPushButton(text, this);
                b->setToolTip(tip);
                connect(b, &QPushButton::clicked, this, [this, action]
                {
                    uint64_t address;
                    if(!evaluate(mSearch->text(), address))
                        mStatus->setText(tr("Adresse invalide"));
                    else if(!action(address))
                        mStatus->setText(tr("Introuvable dans cette direction"));
                });
                searchRow->addWidget(b);
            };
            mSearchThread = new QCheckBox(tr("thread affiché"), this);
            searchButton(tr("◀ Exécution"), tr("Exécution précédente de l'adresse (remonter le temps)"), [this](uint64_t a) { return TtModel::Get().FindExecution(a, false, mSearchThread->isChecked()); });
            searchButton(tr("Exécution ▶"), tr("Exécution suivante de l'adresse"), [this](uint64_t a) { return TtModel::Get().FindExecution(a, true, mSearchThread->isChecked()); });
            searchRow->addWidget(mSearchThread);
            searchButton(tr("◀ Écriture"), tr("Dernière écriture à cette adresse avant l'instant courant"), [](uint64_t a) { return TtModel::Get().FindWrite(a, 1, false); });
            searchButton(tr("Écriture ▶"), tr("Prochaine écriture à cette adresse"), [](uint64_t a) { return TtModel::Get().FindWrite(a, 1, true); });
            mStatus = new QLabel(this);
            searchRow->addWidget(mStatus, 1);
            root->addLayout(searchRow);

            // threads | instructions | registers + memory
            auto* split = new QSplitter(Qt::Horizontal, this);
            auto* threadBox = new QGroupBox(tr("Threads"), split);
            auto* threadLayout = new QVBoxLayout(threadBox);
            mThreads = new QTableWidget(threadBox);
            setupTable(mThreads, { tr("TID"), tr("Instructions"), tr("Début"), tr("Fin") });
            mThreads->setToolTip(tr("Double-clic : voir ce thread au même instant"));
            connect(mThreads, &QTableWidget::cellDoubleClicked, this, [this](int row, int)
            {
                if(auto* item = mThreads->item(row, 0))
                    TtModel::Get().SwitchThread(item->text().toUInt());
            });
            threadLayout->addWidget(mThreads);

            auto* insBox = new QGroupBox(tr("Instructions"), split);
            auto* insLayout = new QVBoxLayout(insBox);
            mOnlyThread = new QCheckBox(tr("Seulement le thread affiché"), insBox);
            connect(mOnlyThread, &QCheckBox::toggled, this, [this] { refresh(); });
            insLayout->addWidget(mOnlyThread);
            mInstructions = new QTableWidget(insBox);
            setupTable(mInstructions, { QStringLiteral("#"), tr("Thread"), tr("Adresse"), tr("Octets"), tr("Instruction"), tr("Symbole") });
            mInstructions->setFont(mono);
            mInstructions->setToolTip(tr("Double-clic : aller à cette instruction"));
            connect(mInstructions, &QTableWidget::cellDoubleClicked, this, [this](int row, int)
            {
                if(auto* item = mInstructions->item(row, 0))
                    TtModel::Get().Seek(item->data(Qt::UserRole).toULongLong());
            });
            insLayout->addWidget(mInstructions);

            auto* right = new QSplitter(Qt::Vertical, split);
            auto* regBox = new QGroupBox(tr("Registres"), right);
            auto* regLayout = new QVBoxLayout(regBox);
            mLevel = new QComboBox(regBox);
            mLevel->addItem(tr("Registres généraux"), (uint)ftt::LevelGpr);
            mLevel->addItem(tr("+ x87"), (uint)(ftt::LevelGpr | ftt::LevelFpu));
            mLevel->addItem(tr("+ SSE"), (uint)(ftt::LevelGpr | ftt::LevelSse));
            mLevel->addItem(tr("Tous"), (uint)ftt::LevelAll);
            connect(mLevel, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this] { refresh(); });
            regLayout->addWidget(mLevel);
            mRegisters = new QTableWidget(regBox);
            setupTable(mRegisters, { tr("Registre"), tr("Valeur") });
            mRegisters->setFont(mono);
            regLayout->addWidget(mRegisters);

            auto* memBox = new QGroupBox(tr("Mémoire à cet instant"), right);
            auto* memLayout = new QVBoxLayout(memBox);
            auto* memRow = new QHBoxLayout();
            mMemoryAddress = new QLineEdit(QStringLiteral("csp"), memBox);
            mMemoryAddress->setToolTip(tr("Adresse, ou registre du thread affiché (csp = pile)"));
            connect(mMemoryAddress, &QLineEdit::returnPressed, this, [this] { refresh(); });
            memRow->addWidget(new QLabel(tr("Adresse :"), memBox));
            memRow->addWidget(mMemoryAddress, 1);
            memLayout->addLayout(memRow);
            mMemory = new QPlainTextEdit(memBox);
            mMemory->setReadOnly(true);
            mMemory->setFont(mono);
            mMemory->setLineWrapMode(QPlainTextEdit::NoWrap);
            memLayout->addWidget(mMemory);
            memLayout->addWidget(new QLabel(tr("?? inconnu · minuscules : valeur lue plus tard (déduite)"), memBox));

            split->setStretchFactor(0, 1);
            split->setStretchFactor(1, 4);
            split->setStretchFactor(2, 2);
            root->addWidget(split, 1);

            connect(open, &QPushButton::clicked, this, [this]
            {
                const QString path = QFileDialog::getOpenFileName(this, tr("Ouvrir une trace"), QString(),
                    tr("Traces (*.ftt *.trace64 *.trace32);;Tous les fichiers (*.*)"));
                if(path.isEmpty())
                    return;
                std::string error;
                if(!TtModel::Get().Open(path.toStdWString(), error))
                    mStatus->setText(tr("Échec : %1").arg(FromUtf8(error)));
                else
                    mStatus->clear();
            });

            // The model can change from the tt.* commands (command thread): refresh on the GUI thread
            QPointer<TimeTravelPanel> self(this);
            TtModel::Get().AddListener([self]
            {
                if(!self || self->mPending.exchange(true))
                    return;
                QMetaObject::invokeMethod(self, [self]
                {
                    if(self)
                    {
                        self->mPending = false;
                        self->refresh();
                    }
                }, Qt::QueuedConnection);
            });
            refresh();
        }

    private:
        static void setupTable(QTableWidget* table, const QStringList & headers)
        {
            table->setColumnCount(headers.size());
            table->setHorizontalHeaderLabels(headers);
            table->verticalHeader()->setVisible(false);
            table->verticalHeader()->setDefaultSectionSize(18);
            table->setSelectionBehavior(QAbstractItemView::SelectRows);
            table->setSelectionMode(QAbstractItemView::SingleSelection);
            table->setEditTriggers(QAbstractItemView::NoEditTriggers);
            table->horizontalHeader()->setStretchLastSection(true);
        }

        // A number (hexadecimal), or a register of the viewed thread at the position
        bool evaluate(const QString & text, uint64_t & value)
        {
            QString t = text.trimmed().toLower();
            if(t.isEmpty())
                return false;
            auto & model = TtModel::Get();
            const unsigned ptr = model.GetInfo().pointerSize;
            if(t == QStringLiteral("csp"))
                t = ptr == 8 ? QStringLiteral("rsp") : QStringLiteral("esp");
            if(t == QStringLiteral("cip"))
                t = ptr == 8 ? QStringLiteral("rip") : QStringLiteral("eip");
            const auto & catalog = ftt::Registers(ptr ? ptr : sizeof(duint));
            ftt::RegisterFile regs;
            for(unsigned id = 0; id < catalog.size(); id++)
                if(t == QLatin1String(catalog[id].name) && model.Registers(regs) && regs.known[id])
                {
                    value = regs.get(id) & (ptr == 8 ? ~0ull : 0xFFFFFFFFull);
                    return true;
                }
            if(t.startsWith(QStringLiteral("0x")))
                t = t.mid(2);
            bool ok = false;
            value = t.toULongLong(&ok, 16);
            return ok;
        }

        void refresh()
        {
            mUpdating = true;
            auto & model = TtModel::Get();
            const auto info = model.GetInfo();
            const bool open = model.IsOpen();
            const uint64_t position = model.Position();
            const uint32_t viewThread = model.ViewThread();
            const unsigned ptr = info.pointerSize ? info.pointerSize : sizeof(duint);
            mShowCpu->setEnabled(open && model.SameProcess());

            if(!open)
            {
                mFile->setText(tr("Aucune trace ouverte"));
                mPosition->setRange(0, 0);
                mWhere->clear();
                mTimeline->setData(0, {}, {}, 0, 0);
                mThreads->setRowCount(0);
                mInstructions->setRowCount(0);
                mRegisters->setRowCount(0);
                mMemory->clear();
                mUpdating = false;
                return;
            }
            mFile->setText(tr("%1 — %2 instructions, %3 threads, %4 instantanés mémoire%5")
                           .arg(QString::fromStdWString(info.path)).arg((qulonglong)info.count).arg((qulonglong)info.threads)
                           .arg((qulonglong)info.snapshots).arg(info.source.empty() ? QString() : tr(" (convertie depuis %1)").arg(QString::fromStdWString(info.source))));
            mPosition->setRange(0, (int)std::min<uint64_t>(info.count ? info.count - 1 : 0, INT_MAX));
            mPosition->setValue((int)std::min<uint64_t>(position, INT_MAX));

            ftt::Instruction current;
            const bool haveCurrent = model.Instruction(position, current);
            mWhere->setText(haveCurrent ? tr("thread %1%2").arg(current.thread)
                            .arg(viewThread != current.thread ? tr(" — thread affiché : %1").arg(viewThread) : QString()) : QString());

            // timelines and threads
            auto threads = model.Threads();
            std::vector<std::vector<std::pair<uint64_t, uint64_t>>> ranges;
            for(auto & t : threads)
                ranges.push_back(model.ThreadRanges(t.thread));
            mTimeline->setData(info.count, threads, ranges, position, viewThread);
            mThreads->setRowCount((int)threads.size());
            for(int row = 0; row < (int)threads.size(); row++)
            {
                const auto & t = threads[row];
                const QString cells[] = { QString::number(t.thread), QString::number((qulonglong)t.count), QString::number((qulonglong)t.firstSeq), QString::number((qulonglong)t.lastSeq) };
                for(int c = 0; c < 4; c++)
                {
                    auto* item = new QTableWidgetItem(cells[c]);
                    item->setBackground(t.thread == viewThread ? QBrush(ThreadColor(row).lighter(130)) : QBrush());
                    QFont f = item->font();
                    f.setBold(t.thread == viewThread);
                    item->setFont(f);
                    mThreads->setItem(row, c, item);
                }
            }

            // instructions around the position
            std::vector<uint64_t> seqs;
            const int before = 20, after = 20;
            if(mOnlyThread->isChecked())
            {
                uint64_t anchor;
                if(model.CurrentInViewThread(anchor))
                {
                    std::vector<uint64_t> back;
                    uint64_t s = anchor;
                    for(int i = 0; i < before && model.NextInViewThread(s, false, s); i++)
                        back.push_back(s);
                    seqs.assign(back.rbegin(), back.rend());
                    seqs.push_back(anchor);
                    s = anchor;
                    for(int i = 0; i < after && model.NextInViewThread(s, true, s); i++)
                        seqs.push_back(s);
                }
            }
            else
            {
                const uint64_t first = position > (uint64_t)before ? position - before : 0;
                for(uint64_t s = first; s < info.count && s <= position + after; s++)
                    seqs.push_back(s);
            }
            mInstructions->setRowCount((int)seqs.size());
            int currentRow = -1;
            for(int row = 0; row < (int)seqs.size(); row++)
            {
                ftt::Instruction ins;
                if(!model.Instruction(seqs[row], ins))
                    continue;
                QString bytes;
                for(int i = 0; i < ins.opcodeSize; i++)
                    bytes += QStringLiteral("%1 ").arg(ins.opcode[i], 2, 16, QChar('0')).toUpper();
                const QString cells[] = { QString::number((qulonglong)ins.seq), QString::number(ins.thread), Hex(ins.ip, ptr), bytes.trimmed(),
                                          FromUtf8(model.Disassemble(ins)), FromUtf8(model.Label(ins.ip))
                                        };
                for(int c = 0; c < 6; c++)
                {
                    auto* item = new QTableWidgetItem(cells[c]);
                    if(c == 0)
                        item->setData(Qt::UserRole, (qulonglong)ins.seq);
                    if(ins.switched)
                    {
                        // The order of the switch is exact when no other thread executed code meanwhile
                        ftt::CpuSample sample;
                        const bool sampled = ins.seq > 0 && model.OrderSample(ins.seq - 1, sample);
                        const bool exact = sampled && sample.exact();
                        item->setBackground(sampled && !exact ? QColor(255, 205, 160) : QColor(255, 236, 179));
                        QString order = !sampled ? tr("ordre non échantillonné")
                                        : exact ? tr("ordre exact : aucun autre thread n'a exécuté de code entre-temps")
                                        : tr("ordre approximatif : d'autres threads ont exécuté du code hors de la trace");
                        if(sampled)
                            for(auto & thread : sample.threads)
                                if(thread.userCode)
                                    order += tr("\nthread %1 : %2 cycles hors trace").arg(thread.thread).arg((qulonglong)thread.cycles);
                        item->setToolTip(tr("Changement de contexte : l'instruction précédente s'exécutait sur le thread %1\n%2").arg(ins.fromThread).arg(order));
                    }
                    if(ins.seq == position)
                    {
                        QFont f = item->font();
                        f.setBold(true);
                        item->setFont(f);
                        currentRow = row;
                    }
                    mInstructions->setItem(row, c, item);
                }
            }
            if(currentRow >= 0)
            {
                mInstructions->selectRow(currentRow);
                mInstructions->scrollToItem(mInstructions->item(currentRow, 0), QAbstractItemView::PositionAtCenter);
            }
            mInstructions->resizeColumnsToContents();

            // registers of the viewed thread, changes highlighted
            ftt::RegisterFile regs, previous;
            uint64_t at = 0;
            const bool haveRegs = model.Registers(regs, &at);
            const bool havePrevious = model.PreviousRegisters(previous);
            const auto & catalog = ftt::Registers(ptr);
            const uint32_t levels = mLevel->currentData().toUInt() & info.levels;
            std::vector<unsigned> shown;
            for(unsigned id = 0; id < catalog.size(); id++)
                if(haveRegs && (catalog[id].level & levels) && regs.known[id])
                    shown.push_back(id);
            mRegisters->setRowCount((int)shown.size());
            for(int row = 0; row < (int)shown.size(); row++)
            {
                const unsigned id = shown[row];
                const bool changed = havePrevious && previous.known[id] && memcmp(previous.value[id], regs.value[id], catalog[id].size) != 0;
                auto* name = new QTableWidgetItem(QString::fromLatin1(catalog[id].name));
                auto* value = new QTableWidgetItem(FromUtf8(ftt::RegisterText(regs, id, catalog[id].size)));
                if(changed)
                {
                    value->setForeground(QColor(220, 0, 0));
                    value->setToolTip(tr("Modifié par l'instruction précédente du thread"));
                }
                mRegisters->setItem(row, 0, name);
                mRegisters->setItem(row, 1, value);
            }

            // memory at this instant
            uint64_t address = 0;
            if(evaluate(mMemoryAddress->text(), address))
            {
                const size_t size = 256;
                address &= ~(uint64_t)15;
                std::vector<uint8_t> bytes;
                std::vector<bool> known, inferred;
                model.Memory(address, size, bytes, known, inferred);
                QString text;
                for(size_t row = 0; row < size; row += 16)
                {
                    text += Hex(address + row, ptr) + QStringLiteral("  ");
                    QString ascii;
                    for(size_t i = row; i < row + 16; i++)
                    {
                        if(!known[i])
                        {
                            text += QStringLiteral("?? ");
                            ascii += QChar('?');
                            continue;
                        }
                        QString hex = QStringLiteral("%1").arg(bytes[i], 2, 16, QChar('0'));
                        text += (inferred[i] ? hex : hex.toUpper()) + QChar(' ');
                        ascii += bytes[i] >= 0x20 && bytes[i] < 0x7F ? QChar(bytes[i]) : QChar('.');
                    }
                    text += QStringLiteral(" ") + ascii + QChar('\n');
                }
                mMemory->setPlainText(text);
            }
            else
                mMemory->setPlainText(tr("Adresse invalide"));
            mUpdating = false;
        }

        QLabel* mFile;
        QSpinBox* mPosition;
        QLabel* mWhere;
        QPushButton* mShowCpu;
        TtTimeline* mTimeline;
        QLineEdit* mSearch;
        QCheckBox* mSearchThread;
        QLabel* mStatus;
        QTableWidget* mThreads;
        QCheckBox* mOnlyThread;
        QTableWidget* mInstructions;
        QComboBox* mLevel;
        QTableWidget* mRegisters;
        QLineEdit* mMemoryAddress;
        QPlainTextEdit* mMemory;
        bool mUpdating = false;
        std::atomic<bool> mPending { false };
    };
}

QWidget* CreateTimeTravelPanel()
{
    return new TimeTravelPanel();
}

#endif // EXTRACMDS_GUI
